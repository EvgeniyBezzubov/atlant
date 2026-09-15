import cv2
import time
import threading
import json
import os
import multiprocessing as mp
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk
import math


os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"


class CameraManager:
    """Класс для управления подключениями к камерам"""

    def __init__(self, config_file='camera_config.json'):
        self.cameras = []
        self.config_file = config_file
        self.load_config()

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.cameras = data.get('cameras', [])
                    print(f"✅ Загружено {len(self.cameras)} камер из конфигурации")
            except Exception as e:
                print(f"❌ Ошибка загрузки конфигурации: {e}")
                self.cameras = []
        else:
            print("ℹ️ Файл конфигурации не найден, создаем новый")
            self.cameras = []
            self.save_config()

    def save_config(self):
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump({'cameras': self.cameras}, f, ensure_ascii=False, indent=2)
            print("✅ Конфигурация сохранена")
        except Exception as e:
            print(f"❌ Ошибка сохранения конфигурации: {e}")

    def add_camera(self, name, ip, port, login, password):
        camera_id = max([c['id'] for c in self.cameras] + [-1]) + 1
        camera = {
            'id': camera_id,
            'name': name,
            'ip': ip,
            'port': port,
            'login': login,
            'password': password,
            'enabled': True
        }
        self.cameras.append(camera)
        self.save_config()
        return camera_id

    def remove_camera(self, camera_id):
        self.cameras = [c for c in self.cameras if c['id'] != camera_id]
        self.save_config()

    def get_cameras(self):
        return self.cameras


def camera_process_worker(camera_info, frame_queue, status_queue, stop_event):
    """
    Отдельный процесс для чтения одной камеры.
    Позволяет обойти глобальную блокировку FFmpeg в OpenCV.
    """
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"

    info = camera_info
    ip = info['ip']
    port = info['port']
    login = info['login']
    password = info['password']
    name = info['name']

    urls = [
        f"rtsp://{login}:{password}@{ip}:{port}/cam/realmonitor?channel=1&subtype=0",
        f"rtsp://{login}:{password}@{ip}:{port}/cam/realmonitor?channel=1&subtype=1",
        f"rtsp://{login}:{password}@{ip}:{port}/streaming/channels/101",
        f"rtsp://{login}:{password}@{ip}:{port}/live",
        f"rtsp://{login}:{password}@{ip}:{port}/streaming/channels/1",
        f"rtsp://{login}:{password}@{ip}:{port}/h264",
        f"rtsp://{login}:{password}@{ip}:{port}/h265",
    ]

    cap = None
    connected = False

    def try_connect():
        nonlocal cap, connected
        for url in urls:
            if stop_event.is_set():
                return False
            try:
                print(f"  📹 [{name}] Подключение: {url}")
                c = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
                c.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if c.isOpened():
                    ret, frame = c.read()
                    if ret and frame is not None:
                        cap = c
                        connected = True
                        status_queue.put(('connected', True))
                        print(f"  ✅ [{name}] подключена")
                        return True
                c.release()
            except Exception as e:
                print(f"  ❌ [{name}] {e}")
            time.sleep(0.3)
        return False

    # Первичное подключение
    while not stop_event.is_set() and not connected:
        if try_connect():
            break
        time.sleep(2)

    last_frame_time = time.time()
    frame_count = 0
    start_time = time.time()
    no_frame_count = 0

    while not stop_event.is_set():
        try:
            if cap is None or not connected:
                status_queue.put(('connected', False))
                time.sleep(1)
                connected = False
                while not stop_event.is_set() and not connected:
                    if try_connect():
                        break
                    time.sleep(2)
                last_frame_time = time.time()
                no_frame_count = 0
                continue

            ret, frame = cap.read()
            if ret and frame is not None:
                try:
                    if frame_queue.full():
                        try:
                            frame_queue.get_nowait()
                        except Exception:
                            pass
                    frame_queue.put_nowait((frame, time.time()))
                except Exception:
                    pass

                no_frame_count = 0
                frame_count += 1
                last_frame_time = time.time()
            else:
                no_frame_count += 1
                if time.time() - last_frame_time > 5 and no_frame_count > 10:
                    print(f"🔄 [{name}] Переподключение...")
                    try:
                        cap.release()
                    except Exception:
                        pass
                    cap = None
                    connected = False
                    status_queue.put(('connected', False))
                    no_frame_count = 0

            time.sleep(0.001)

        except Exception as e:
            print(f"❌ [{name}] ошибка чтения: {e}")
            time.sleep(1)

    if cap is not None:
        try:
            cap.release()
        except Exception:
            pass


class CameraFeed:
    """
    Обертка над процессом камеры.
    Главный процесс читает кадры из очереди — это быстро и не блокирует UI.
    """

    def __init__(self, camera_info):
        self.camera_info = camera_info
        self.frame = None
        self.frame_lock = threading.Lock()
        self.connected = False
        self.is_running = False
        self.last_frame_time = 0
        self.fps = 0
        self.frame_count = 0
        self.start_time = None

        self.frame_queue = mp.Queue(maxsize=2)
        self.status_queue = mp.Queue(maxsize=10)
        self.stop_event = mp.Event()
        self.process = None
        self.reader_thread = None

    def start(self):
        if self.is_running:
            return True

        self.is_running = True
        self.start_time = time.time()

        self.process = mp.Process(
            target=camera_process_worker,
            args=(self.camera_info, self.frame_queue, self.status_queue, self.stop_event),
            daemon=True
        )
        self.process.start()

        self.reader_thread = threading.Thread(target=self._read_queue, daemon=True)
        self.reader_thread.start()
        return True

    def _read_queue(self):
        while self.is_running:
            try:
                while True:
                    msg = self.status_queue.get_nowait()
                    if msg[0] == 'connected':
                        self.connected = msg[1]
            except Exception:
                pass

            try:
                frame, ts = self.frame_queue.get(timeout=0.05)
                with self.frame_lock:
                    self.frame = frame
                self.last_frame_time = ts
                self.frame_count += 1
                if self.frame_count % 30 == 0:
                    elapsed = time.time() - self.start_time
                    if elapsed > 0:
                        self.fps = self.frame_count / elapsed
            except Exception:
                pass

            time.sleep(0.001)

    def get_frame(self):
        with self.frame_lock:
            return self.frame.copy() if self.frame is not None else None

    def stop(self):
        self.is_running = False
        self.stop_event.set()
        if self.reader_thread:
            self.reader_thread.join(timeout=1)
        if self.process:
            self.process.join(timeout=2)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=1)
        try:
            while not self.frame_queue.empty():
                self.frame_queue.get_nowait()
        except Exception:
            pass
        try:
            while not self.status_queue.empty():
                self.status_queue.get_nowait()
        except Exception:
            pass
        self.connected = False


class DetachedWindow:
    """Отдельное окно с одним видеопотоком"""

    def __init__(self, parent_app, camera_info, feed):
        self.parent_app = parent_app
        self.camera_info = camera_info
        self.feed = feed
        self.camera_id = camera_info['id']
        self._image = None
        self._running = True

        self.win = tk.Toplevel(parent_app.root)
        self.win.title(f"Камера: {camera_info['name']}")
        self.win.geometry("800x600")
        self.win.minsize(320, 240)
        self.win.configure(bg='black')

        toolbar = tk.Frame(self.win, bg='lightgray', height=30)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        tk.Button(toolbar, text="↩ Вернуть в сетку",
                  command=self.close).pack(side=tk.LEFT, padx=5, pady=2)

        self.info_var = tk.BooleanVar(value=True)
        tk.Checkbutton(toolbar, text="📊 Информация",
                       variable=self.info_var, bg='lightgray').pack(side=tk.LEFT, padx=10)

        self.status_label = tk.Label(toolbar, text="", bg='lightgray')
        self.status_label.pack(side=tk.RIGHT, padx=10)

        self.canvas = tk.Canvas(self.win, bg='black', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self._update()

    def _update(self):
        if not self._running:
            return

        try:
            w = self.canvas.winfo_width()
            h = self.canvas.winfo_height()
            if w < 10 or h < 10:
                self.win.after(50, self._update)
                return

            frame = self.feed.get_frame()
            self.canvas.delete("all")

            if frame is not None:
                fh, fw = frame.shape[:2]
                scale = min(w / fw, h / fh)
                new_w = max(1, int(fw * scale))
                new_h = max(1, int(fh * scale))
                resized = cv2.resize(frame, (new_w, new_h))
                rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

                if self.info_var.get():
                    name = self.camera_info['name']
                    status = f"✅ {self.feed.fps:.1f} FPS" if self.feed.connected else "❌ Отключено"
                    cv2.putText(rgb, name, (10, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    cv2.putText(rgb, status, (10, 60),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 1)
                    cv2.putText(rgb, datetime.now().strftime('%H:%M:%S'),
                                (10, 85), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

                img = Image.fromarray(rgb)
                imgtk = ImageTk.PhotoImage(image=img)
                self.canvas.create_image(w // 2, h // 2,
                                         anchor=tk.CENTER, image=imgtk)
                self._image = imgtk
                self.status_label.config(text=f"{self.camera_info['name']} — {self.feed.fps:.1f} FPS")
            else:
                self.canvas.create_text(w // 2, h // 2,
                                        text=f"{self.camera_info['name']}\nНет сигнала",
                                        fill='white',
                                        font=('Arial', 16),
                                        justify=tk.CENTER)
                self.status_label.config(text=f"{self.camera_info['name']} — нет сигнала")

        except Exception as e:
            print(f"❌ DetachedWindow [{self.camera_info['name']}]: {e}")

        self.win.after(33, self._update)

    def close(self):
        """Вернуть камеру в сетку"""
        self._running = False
        try:
            self.win.destroy()
        except Exception:
            pass
        self.parent_app.detached.pop(self.camera_id, None)


class CameraGridApp:
    """Главное приложение с сеткой камер"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Система видеонаблюдения RTSP")
        self.root.geometry("1200x800")
        self.root.minsize(400, 300)

        self.camera_manager = CameraManager()
        self.feeds = {}
        self.display_cameras = []
        self._images = []
        self.detached = {}   # camera_id -> DetachedWindow

        self.create_menu()
        self.create_toolbar()
        self.create_status_bar()

        self.canvas_frame = tk.Frame(self.root, bg='black')
        self.canvas_frame.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(self.canvas_frame, bg='black', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Привязка двойного клика — вытащить камеру в отдельное окно
        self.canvas.bind("<Double-Button-1>", self._on_canvas_double_click)

        self.load_cameras()

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.root.bind('<Configure>', self.on_resize)

        self.update_video()

    def create_menu(self):
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        camera_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Камеры", menu=camera_menu)
        camera_menu.add_command(label="Добавить камеру", command=self.add_camera_dialog)
        camera_menu.add_command(label="Управление камерами", command=self.manage_cameras_dialog)
        camera_menu.add_separator()
        camera_menu.add_command(label="Обновить все", command=self.reload_cameras)

        view_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Вид", menu=view_menu)
        view_menu.add_command(label="Показать информацию", command=self.toggle_info)

        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Помощь", menu=help_menu)
        help_menu.add_command(label="О программе", command=self.show_about)

    def create_toolbar(self):
        toolbar = tk.Frame(self.root, bg='lightgray', height=40)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        tk.Button(toolbar, text="➕ Добавить камеру",
                  command=self.add_camera_dialog).pack(side=tk.LEFT, padx=5, pady=5)

        tk.Button(toolbar, text="🔄 Обновить",
                  command=self.reload_cameras).pack(side=tk.LEFT, padx=5, pady=5)

        self.info_var = tk.BooleanVar(value=True)
        tk.Checkbutton(toolbar, text="📊 Информация",
                       variable=self.info_var,
                       command=self.toggle_info,
                       bg='lightgray').pack(side=tk.LEFT, padx=10)

        tk.Button(toolbar, text="⛶ На весь экран",
                  command=self.toggle_fullscreen).pack(side=tk.LEFT, padx=5)

        self.status_label = tk.Label(toolbar, text="Готов", bg='lightgray')
        self.status_label.pack(side=tk.RIGHT, padx=10)

        self.cam_counter = tk.Label(toolbar, text="Камер: 0", bg='lightgray')
        self.cam_counter.pack(side=tk.RIGHT, padx=10)

    def create_status_bar(self):
        self.status_bar = tk.Label(self.root, text="Готов к работе",
                                   bd=1, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def toggle_info(self):
        pass

    def toggle_fullscreen(self):
        self.root.attributes('-fullscreen', not self.root.attributes('-fullscreen'))

    def on_resize(self, event):
        if event.widget == self.root:
            self.canvas.config(width=event.width, height=event.height - 80)

    def load_cameras(self):
        self.status_bar.config(text="Загрузка камер...")

        for feed in self.feeds.values():
            feed.stop()
        self.feeds.clear()
        self.display_cameras = []

        for camera_info in self.camera_manager.get_cameras():
            if camera_info.get('enabled', True):
                self.display_cameras.append(camera_info)
                self.add_camera_feed(camera_info)

        self.update_counter()
        self.status_bar.config(
            text=f"Загружено {len(self.display_cameras)} камер (подключение в фоне...)"
        )

    def add_camera_feed(self, camera_info):
        camera_id = camera_info['id']
        if camera_id in self.feeds:
            return
        feed = CameraFeed(camera_info)
        feed.start()
        self.feeds[camera_id] = feed
        self.status_bar.config(text=f"Подключение: {camera_info['name']}...")

    def reload_cameras(self):
        # Закрываем все отдельные окна
        for win in list(self.detached.values()):
            try:
                win._running = False
                win.win.destroy()
            except Exception:
                pass
        self.detached.clear()

        for feed in self.feeds.values():
            feed.stop()
        self.feeds.clear()
        self.display_cameras = []
        self.load_cameras()

    def update_counter(self):
        total = len(self.display_cameras)
        active = sum(1 for feed in self.feeds.values() if feed.connected)
        self.cam_counter.config(text=f"Камер: {active}/{total}")

    # ---------- Логика вытаскивания камеры ----------

    def _get_camera_id_at(self, x, y):
        """Определяет camera_id по координатам клика на canvas"""
        if not self.display_cameras:
            return None

        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()
        if canvas_width < 10 or canvas_height < 10:
            return None

        num_cameras = len(self.display_cameras)
        cols = math.ceil(math.sqrt(num_cameras))
        rows = math.ceil(num_cameras / cols)

        margin = 2
        cell_width = (canvas_width - margin * (cols + 1)) // cols
        cell_height = (canvas_height - margin * (rows + 1)) // rows

        for idx, camera_info in enumerate(self.display_cameras):
            row = idx // cols
            col = idx % cols
            x1 = margin + col * (cell_width + margin)
            y1 = margin + row * (cell_height + margin)
            x2 = x1 + cell_width
            y2 = y1 + cell_height
            if x1 <= x <= x2 and y1 <= y <= y2:
                return camera_info['id']
        return None

    def _on_canvas_double_click(self, event):
        """Двойной клик по ячейке — вытащить камеру в отдельное окно"""
        camera_id = self._get_camera_id_at(event.x, event.y)
        if camera_id is None:
            return
        if camera_id in self.detached:
            try:
                self.detached[camera_id].win.lift()
            except Exception:
                pass
            return
        self.detach_camera(camera_id)

    def detach_camera(self, camera_id):
        """Вынести камеру в отдельное окно"""
        feed = self.feeds.get(camera_id)
        if feed is None:
            return
        camera_info = next((c for c in self.display_cameras if c['id'] == camera_id), None)
        if camera_info is None:
            return

        win = DetachedWindow(self, camera_info, feed)
        self.detached[camera_id] = win
        self.status_bar.config(text=f"Камера '{camera_info['name']}' вынесена в отдельное окно")

    # ---------- Отрисовка сетки ----------

    def update_video(self):
        if not self.display_cameras:
            self.canvas.delete("all")
            self._images.clear()
            self.canvas.create_text(
                self.canvas.winfo_width() // 2,
                self.canvas.winfo_height() // 2,
                text="Нет подключенных камер\nНажмите 'Добавить камеру'",
                fill='white',
                font=('Arial', 20),
                justify=tk.CENTER
            )
            self.root.after(100, self.update_video)
            return

        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()
        if canvas_width < 10 or canvas_height < 10:
            self.root.after(100, self.update_video)
            return

        num_cameras = len(self.display_cameras)
        cols = math.ceil(math.sqrt(num_cameras))
        rows = math.ceil(num_cameras / cols)

        margin = 2
        cell_width = (canvas_width - margin * (cols + 1)) // cols
        cell_height = (canvas_height - margin * (rows + 1)) // rows

        self.canvas.delete("all")
        self._images.clear()

        for idx, camera_info in enumerate(self.display_cameras):
            row = idx // cols
            col = idx % cols
            x1 = margin + col * (cell_width + margin)
            y1 = margin + row * (cell_height + margin)
            x2 = x1 + cell_width
            y2 = y1 + cell_height

            # Если камера вынесена — рисуем заглушку
            if camera_info['id'] in self.detached:
                self.canvas.create_rectangle(x1, y1, x2, y2,
                                             fill='#1a1a2b', outline='#444', width=1)
                self.canvas.create_text(x1 + cell_width // 2, y1 + cell_height // 2,
                                        text=f"{camera_info['name']}\n(в отдельном окне)\n\nДвойной клик — вернуть фокус",
                                        fill='#88aaff',
                                        font=('Arial', 11),
                                        justify=tk.CENTER)
                continue

            feed = self.feeds.get(camera_info['id'])
            frame = feed.get_frame() if feed is not None else None

            if frame is not None:
                h, w = frame.shape[:2]
                scale = min(cell_width / w, cell_height / h)
                new_w = int(w * scale)
                new_h = int(h * scale)
                resized = cv2.resize(frame, (new_w, new_h))
                rgb_frame = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

                if self.info_var.get():
                    info_text = f"{camera_info['name']}"
                    status_text = f"✅ {feed.fps:.1f} FPS" if feed.connected else "❌ Отключено"
                    cv2.putText(rgb_frame, info_text, (10, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                    cv2.putText(rgb_frame, status_text, (10, 55),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
                    cv2.putText(rgb_frame, datetime.now().strftime('%H:%M:%S'),
                                (10, 75), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

                img = Image.fromarray(rgb_frame)
                imgtk = ImageTk.PhotoImage(image=img)

                self.canvas.create_image(x1 + cell_width // 2, y1 + cell_height // 2,
                                         anchor=tk.CENTER, image=imgtk)
                self._images.append(imgtk)
            else:
                self.canvas.create_rectangle(x1, y1, x2, y2,
                                             fill='#2b2b2b', outline='#444', width=2)
                status = "Подключение..." if feed is not None else "Нет сигнала"
                self.canvas.create_text(x1 + cell_width // 2, y1 + cell_height // 2,
                                        text=f"{camera_info['name']}\n{status}",
                                        fill='white',
                                        font=('Arial', 12),
                                        justify=tk.CENTER)

            self.canvas.create_rectangle(x1, y1, x2, y2, outline='#444', width=1)

        active_cams = sum(1 for feed in self.feeds.values() if feed.connected)
        total_cams = len(self.display_cameras)
        self.status_bar.config(text=f"Камер: {active_cams}/{total_cams} активны")
        self.cam_counter.config(text=f"Камер: {active_cams}/{total_cams}")

        self.root.after(30, self.update_video)

    # ---------- Диалоги ----------

    def add_camera_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Добавить камеру")
        dialog.geometry("400x350")
        dialog.transient(self.root)
        dialog.grab_set()

        fields = [
            ('Название:', 'entry', 'Камера'),
            ('IP адрес:', 'entry', ''),
            ('Порт:', 'entry', '554'),
            ('Логин:', 'entry', 'admin'),
            ('Пароль:', 'entry', ''),
        ]

        entries = {}
        for i, (label, type_, default) in enumerate(fields):
            tk.Label(dialog, text=label).grid(row=i, column=0, padx=10, pady=5, sticky='e')
            if type_ == 'entry':
                entry = tk.Entry(dialog, width=25, show='*' if 'Пароль' in label else '')
                entry.insert(0, default)
                entry.grid(row=i, column=1, padx=10, pady=5, sticky='w')
                entries[label] = entry

        def save_camera():
            try:
                name = entries['Название:'].get()
                ip = entries['IP адрес:'].get()
                port = int(entries['Порт:'].get())
                login = entries['Логин:'].get()
                password = entries['Пароль:'].get()

                if not ip:
                    messagebox.showerror("Ошибка", "Введите IP адрес")
                    return

                camera_id = self.camera_manager.add_camera(name, ip, port, login, password)

                for cam in self.camera_manager.get_cameras():
                    if cam['id'] == camera_id:
                        self.display_cameras.append(cam)
                        self.add_camera_feed(cam)
                        break

                self.update_counter()
                dialog.destroy()
                self.status_bar.config(text=f"Камера {name} добавлена")

            except ValueError:
                messagebox.showerror("Ошибка", "Неверный формат порта")
            except Exception as e:
                messagebox.showerror("Ошибка", f"Ошибка добавления: {e}")

        tk.Button(dialog, text="Добавить", command=save_camera).grid(row=len(fields), column=0, pady=20)
        tk.Button(dialog, text="Отмена", command=dialog.destroy).grid(row=len(fields), column=1, pady=20)

    def manage_cameras_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Управление камерами")
        dialog.geometry("600x400")
        dialog.transient(self.root)
        dialog.grab_set()

        tree = ttk.Treeview(dialog, columns=('ID', 'Название', 'IP', 'Порт', 'Статус'), show='headings')
        tree.heading('ID', text='ID')
        tree.heading('Название', text='Название')
        tree.heading('IP', text='IP адрес')
        tree.heading('Порт', text='Порт')
        tree.heading('Статус', text='Статус')
        tree.column('ID', width=50)
        tree.column('Название', width=150)
        tree.column('IP', width=150)
        tree.column('Порт', width=80)
        tree.column('Статус', width=100)
        tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        def fill_tree():
            for item in tree.get_children():
                tree.delete(item)
            for cam in self.camera_manager.get_cameras():
                feed = self.feeds.get(cam['id'])
                status = "✅ Активна" if feed and feed.connected else "❌ Отключена"
                tree.insert('', 'end', values=(cam['id'], cam['name'], cam['ip'], cam['port'], status))

        fill_tree()

        btn_frame = tk.Frame(dialog)
        btn_frame.pack(pady=10)

        def delete_camera():
            selection = tree.selection()
            if not selection:
                messagebox.showwarning("Предупреждение", "Выберите камеру")
                return

            if messagebox.askyesno("Подтверждение", "Удалить выбранную камеру?"):
                item = tree.item(selection[0])
                camera_id = item['values'][0]

                # Если вынесена — закрываем отдельное окно
                if camera_id in self.detached:
                    try:
                        self.detached[camera_id]._running = False
                        self.detached[camera_id].win.destroy()
                    except Exception:
                        pass
                    del self.detached[camera_id]

                self.camera_manager.remove_camera(camera_id)

                if camera_id in self.feeds:
                    self.feeds[camera_id].stop()
                    del self.feeds[camera_id]

                self.display_cameras = [c for c in self.display_cameras if c['id'] != camera_id]

                tree.delete(selection[0])
                self.update_counter()
                self.status_bar.config(text=f"Камера {camera_id} удалена")

        tk.Button(btn_frame, text="❌ Удалить", command=delete_camera).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="🔄 Обновить", command=fill_tree).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Закрыть", command=dialog.destroy).pack(side=tk.RIGHT, padx=5)

    def show_about(self):
        messagebox.showinfo(
            "О программе",
            "Система видеонаблюдения RTSP\n"
            "Версия 3.3\n\n"
            "Поддержка камер Dahua и других RTSP камер\n"
            "Каждая камера читается в отдельном процессе\n"
            "Двойной клик по ячейке — вынести камеру в отдельное окно\n"
            "Автоматическое переподключение при обрыве"
        )

    def on_closing(self):
        # Закрываем все отдельные окна
        for win in list(self.detached.values()):
            try:
                win._running = False
                win.win.destroy()
            except Exception:
                pass
        self.detached.clear()

        for feed in self.feeds.values():
            feed.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    mp.freeze_support()
    app = CameraGridApp()
    app.run()