import cv2
import time
import threading
import json
import os
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk
import socket
import math


class CameraManager:
    """Класс для управления подключениями к камерам"""

    def __init__(self, config_file='camera_config.json'):
        self.cameras = []
        self.config_file = config_file
        self.load_config()

    def load_config(self):
        """Загрузка конфигурации из файла"""
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
        """Сохранение конфигурации в файл"""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump({'cameras': self.cameras}, f, ensure_ascii=False, indent=2)
            print("✅ Конфигурация сохранена")
        except Exception as e:
            print(f"❌ Ошибка сохранения конфигурации: {e}")

    def add_camera(self, name, ip, port, login, password):
        """Добавление новой камеры"""
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
        """Удаление камеры"""
        self.cameras = [c for c in self.cameras if c['id'] != camera_id]
        self.save_config()

    def get_cameras(self):
        """Получение списка камер"""
        return self.cameras


class CameraFeed:
    """Класс для работы с видеопотоком камеры"""

    def __init__(self, camera_info):
        self.camera_info = camera_info
        self.cap = None
        self.frame = None
        self.frame_lock = threading.Lock()
        self.is_running = False
        self.thread = None
        self.connected = False
        self.retry_count = 0
        self.last_frame_time = 0
        self.fps = 0
        self.frame_count = 0
        self.start_time = None
        self.rtsp_urls = []
        self.current_url_index = 0
        self.build_rtsp_urls()

    def build_rtsp_urls(self):
        """Формирование возможных RTSP URL"""
        info = self.camera_info
        ip = info['ip']
        port = info['port']
        login = info['login']
        password = info['password']

        self.rtsp_urls = [
            f"rtsp://{login}:{password}@{ip}:{port}/cam/realmonitor?channel=1&subtype=0",
            f"rtsp://{login}:{password}@{ip}:{port}/cam/realmonitor?channel=1&subtype=1",
            f"rtsp://{login}:{password}@{ip}:{port}/streaming/channels/101",
            f"rtsp://{login}:{password}@{ip}:{port}/live",
            f"rtsp://{login}:{password}@{ip}:{port}/streaming/channels/1",
            f"rtsp://{login}:{password}@{ip}:{port}/h264",
            f"rtsp://{login}:{password}@{ip}:{port}/h265",
        ]
        self.current_url_index = 0

    def connect(self):
        """Подключение к камере"""
        if self.connected:
            return True

        for i in range(self.current_url_index, len(self.rtsp_urls)):
            url = self.rtsp_urls[i]
            try:
                print(f"  📹 Подключение к {self.camera_info['name']}: {url}")
                self.cap = cv2.VideoCapture(url)
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)

                if self.cap.isOpened():
                    # Проверяем наличие кадра
                    ret, frame = self.cap.read()
                    if ret and frame is not None:
                        self.connected = True
                        self.current_url_index = i
                        print(f"  ✅ {self.camera_info['name']} подключена")
                        return True

                self.cap.release()
                self.cap = None

            except Exception as e:
                print(f"  ❌ Ошибка подключения {self.camera_info['name']}: {e}")

            time.sleep(0.5)

        print(f"  ❌ {self.camera_info['name']} - не удалось подключиться")
        return False

    def start(self):
        """Запуск потока"""
        if not self.connect():
            return False

        self.is_running = True
        self.start_time = time.time()
        self.thread = threading.Thread(target=self._update_frame, daemon=True)
        self.thread.start()
        return True

    def _update_frame(self):
        """Обновление кадра в отдельном потоке"""
        no_frame_count = 0

        while self.is_running:
            try:
                if self.cap is None:
                    self.reconnect()
                    continue

                ret, frame = self.cap.read()
                if ret and frame is not None:
                    with self.frame_lock:
                        self.frame = frame
                    self.retry_count = 0
                    no_frame_count = 0
                    self.last_frame_time = time.time()
                    self.frame_count += 1

                    if self.frame_count % 30 == 0:
                        elapsed = time.time() - self.start_time
                        self.fps = self.frame_count / elapsed

                else:
                    no_frame_count += 1
                    self.retry_count += 1

                    if no_frame_count == 1:
                        print(f"⚠️ {self.camera_info['name']}: Нет кадра (попытка {self.retry_count})")

                    if time.time() - self.last_frame_time > 5 and no_frame_count > 10:
                        print(f"🔄 {self.camera_info['name']}: Переподключение...")
                        self.reconnect()
                        no_frame_count = 0
                        self.retry_count = 0

                time.sleep(0.001)

            except Exception as e:
                print(f"❌ {self.camera_info['name']}: Ошибка - {e}")
                time.sleep(1)

        if self.cap:
            self.cap.release()

    def reconnect(self):
        """Переподключение"""
        if self.cap:
            self.cap.release()
            self.cap = None
        self.connected = False
        time.sleep(1)
        self.connect()

    def get_frame(self):
        """Получение текущего кадра"""
        with self.frame_lock:
            return self.frame.copy() if self.frame is not None else None

    def stop(self):
        """Остановка потока"""
        self.is_running = False
        if self.thread:
            self.thread.join(timeout=2)
        if self.cap:
            self.cap.release()
        self.connected = False


class CameraGridApp:
    """Главное приложение с сеткой камер"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Система видеонаблюдения RTSP")
        self.root.geometry("1200x800")
        self.root.minsize(400, 300)

        # Создаем менеджеры
        self.camera_manager = CameraManager()
        self.feeds = {}  # camera_id -> CameraFeed

        # Создаем интерфейс
        self.create_menu()
        self.create_toolbar()
        self.create_status_bar()

        # Основной Canvas для отображения сетки
        self.canvas_frame = tk.Frame(self.root, bg='black')
        self.canvas_frame.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(self.canvas_frame, bg='black', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Загружаем камеры
        self.load_cameras()

        # Настраиваем обновление
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.root.bind('<Configure>', self.on_resize)

        # Запускаем обновление видео
        self.update_video()

    def create_menu(self):
        """Создание меню"""
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        # Меню "Камеры"
        camera_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Камеры", menu=camera_menu)
        camera_menu.add_command(label="Добавить камеру", command=self.add_camera_dialog)
        camera_menu.add_command(label="Управление камерами", command=self.manage_cameras_dialog)
        camera_menu.add_separator()
        camera_menu.add_command(label="Обновить все", command=self.reload_cameras)

        # Меню "Вид"
        view_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Вид", menu=view_menu)
        view_menu.add_command(label="Показать информацию", command=self.toggle_info)

        # Меню "Помощь"
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Помощь", menu=help_menu)
        help_menu.add_command(label="О программе", command=self.show_about)

    def create_toolbar(self):
        """Создание панели инструментов"""
        toolbar = tk.Frame(self.root, bg='lightgray', height=40)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        # Кнопка добавления камеры
        btn_add = tk.Button(toolbar, text="➕ Добавить камеру",
                            command=self.add_camera_dialog)
        btn_add.pack(side=tk.LEFT, padx=5, pady=5)

        # Кнопка обновления
        btn_refresh = tk.Button(toolbar, text="🔄 Обновить",
                                command=self.reload_cameras)
        btn_refresh.pack(side=tk.LEFT, padx=5, pady=5)

        # Кнопка показать/скрыть информацию
        self.info_var = tk.BooleanVar(value=True)
        btn_info = tk.Checkbutton(toolbar, text="📊 Информация",
                                  variable=self.info_var,
                                  command=self.toggle_info,
                                  bg='lightgray')
        btn_info.pack(side=tk.LEFT, padx=10)

        # Кнопка полноэкранного режима
        btn_fullscreen = tk.Button(toolbar, text="⛶ На весь экран",
                                   command=self.toggle_fullscreen)
        btn_fullscreen.pack(side=tk.LEFT, padx=5)

        # Статус
        self.status_label = tk.Label(toolbar, text="Готов", bg='lightgray')
        self.status_label.pack(side=tk.RIGHT, padx=10)

        # Счетчик камер
        self.cam_counter = tk.Label(toolbar, text="Камер: 0", bg='lightgray')
        self.cam_counter.pack(side=tk.RIGHT, padx=10)

    def create_status_bar(self):
        """Создание строки статуса"""
        self.status_bar = tk.Label(self.root, text="Готов к работе",
                                   bd=1, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def toggle_info(self):
        """Переключение отображения информации"""
        # Просто обновляем, информация будет отображаться на кадрах
        pass

    def toggle_fullscreen(self):
        """Переключение полноэкранного режима"""
        self.root.attributes('-fullscreen', not self.root.attributes('-fullscreen'))

    def on_resize(self, event):
        """Обработка изменения размера окна"""
        if event.widget == self.root:
            self.canvas.config(width=event.width, height=event.height - 80)

    def load_cameras(self):
        """Загрузка всех камер"""
        self.status_bar.config(text="Загрузка камер...")

        # Очищаем старые потоки
        for feed in self.feeds.values():
            feed.stop()
        self.feeds.clear()

        # Загружаем камеры
        for camera_info in self.camera_manager.get_cameras():
            if camera_info.get('enabled', True):
                self.add_camera_feed(camera_info)

        self.update_counter()
        self.status_bar.config(text=f"Загружено {len(self.feeds)} камер")

    def add_camera_feed(self, camera_info):
        """Добавление потока камеры"""
        camera_id = camera_info['id']

        if camera_id in self.feeds:
            return

        # Создаем поток
        feed = CameraFeed(camera_info)
        if feed.start():
            self.feeds[camera_id] = feed
            self.status_bar.config(text=f"Подключена: {camera_info['name']}")
        else:
            self.status_bar.config(text=f"Не удалось подключить: {camera_info['name']}")

    def reload_cameras(self):
        """Перезагрузка всех камер"""
        # Останавливаем все потоки
        for feed in self.feeds.values():
            feed.stop()
        self.feeds.clear()

        # Загружаем заново
        self.load_cameras()

    def update_counter(self):
        """Обновление счетчика камер"""
        count = len(self.feeds)
        self.cam_counter.config(text=f"Камер: {count}")

    def update_video(self):
        """Обновление видеопотоков в сетке"""
        if not self.feeds:
            # Если нет камер, показываем сообщение
            self.canvas.delete("all")
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

        # Количество камер
        num_cameras = len(self.feeds)

        # Рассчитываем сетку
        cols = math.ceil(math.sqrt(num_cameras))
        rows = math.ceil(num_cameras / cols)

        # Размер каждой ячейки
        margin = 2
        cell_width = (canvas_width - margin * (cols + 1)) // cols
        cell_height = (canvas_height - margin * (rows + 1)) // rows

        # Очищаем canvas
        self.canvas.delete("all")

        # Отображаем каждую камеру
        for idx, (camera_id, feed) in enumerate(self.feeds.items()):
            row = idx // cols
            col = idx % cols

            x1 = margin + col * (cell_width + margin)
            y1 = margin + row * (cell_height + margin)
            x2 = x1 + cell_width
            y2 = y1 + cell_height

            # Получаем кадр
            frame = feed.get_frame()

            if frame is not None:
                # Масштабируем кадр
                h, w = frame.shape[:2]
                scale = min(cell_width / w, cell_height / h)
                new_w = int(w * scale)
                new_h = int(h * scale)

                resized = cv2.resize(frame, (new_w, new_h))

                # Конвертируем в RGB
                rgb_frame = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

                # Добавляем информацию на кадр
                if self.info_var.get():
                    # Информация о камере
                    info_text = f"{feed.camera_info['name']}"
                    status_text = f"✅ {feed.fps:.1f} FPS" if feed.connected else "❌ Отключено"

                    # Добавляем текст поверх кадра
                    cv2.putText(rgb_frame, info_text, (10, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                    cv2.putText(rgb_frame, status_text, (10, 55),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
                    cv2.putText(rgb_frame, datetime.now().strftime('%H:%M:%S'),
                                (10, 75), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

                # Создаем ImageTk
                img = Image.fromarray(rgb_frame)
                imgtk = ImageTk.PhotoImage(image=img)

                # Отображаем на canvas
                self.canvas.create_image(x1 + cell_width // 2, y1 + cell_height // 2,
                                         anchor=tk.CENTER, image=imgtk)
                self.canvas.image = imgtk  # Сохраняем ссылку

            else:
                # Если нет кадра, показываем заглушку
                self.canvas.create_rectangle(x1, y1, x2, y2, fill='#2b2b2b', outline='#444', width=2)
                self.canvas.create_text(x1 + cell_width // 2, y1 + cell_height // 2,
                                        text=f"{feed.camera_info['name']}\nНет сигнала",
                                        fill='white',
                                        font=('Arial', 12),
                                        justify=tk.CENTER)

            # Рамка ячейки
            self.canvas.create_rectangle(x1, y1, x2, y2, outline='#444', width=1)

        # Обновляем статус
        active_cams = sum(1 for feed in self.feeds.values() if feed.connected)
        total_cams = len(self.feeds)
        self.status_bar.config(text=f"Камер: {active_cams}/{total_cams} активны")

        # Планируем следующее обновление
        self.root.after(30, self.update_video)

    def add_camera_dialog(self):
        """Диалог добавления камеры"""
        dialog = tk.Toplevel(self.root)
        dialog.title("Добавить камеру")
        dialog.geometry("400x350")
        dialog.transient(self.root)
        dialog.grab_set()

        # Поля ввода
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

        # Кнопки
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

                # Добавляем камеру
                camera_id = self.camera_manager.add_camera(name, ip, port, login, password)

                # Находим добавленную камеру
                for cam in self.camera_manager.get_cameras():
                    if cam['id'] == camera_id:
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
        """Диалог управления камерами"""
        dialog = tk.Toplevel(self.root)
        dialog.title("Управление камерами")
        dialog.geometry("600x400")
        dialog.transient(self.root)
        dialog.grab_set()

        # Создаем список
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

        # Заполняем список
        for cam in self.camera_manager.get_cameras():
            feed = self.feeds.get(cam['id'])
            status = "✅ Активна" if feed and feed.connected else "❌ Отключена"
            tree.insert('', 'end', values=(cam['id'], cam['name'], cam['ip'], cam['port'], status))

        # Кнопки управления
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

                # Удаляем из менеджера
                self.camera_manager.remove_camera(camera_id)

                # Останавливаем поток
                if camera_id in self.feeds:
                    self.feeds[camera_id].stop()
                    del self.feeds[camera_id]

                tree.delete(selection[0])
                self.update_counter()
                self.status_bar.config(text=f"Камера {camera_id} удалена")

        def refresh_list():
            # Обновляем список
            for item in tree.get_children():
                tree.delete(item)

            for cam in self.camera_manager.get_cameras():
                feed = self.feeds.get(cam['id'])
                status = "✅ Активна" if feed and feed.connected else "❌ Отключена"
                tree.insert('', 'end', values=(cam['id'], cam['name'], cam['ip'], cam['port'], status))

        tk.Button(btn_frame, text="❌ Удалить", command=delete_camera).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="🔄 Обновить", command=refresh_list).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Закрыть", command=dialog.destroy).pack(side=tk.RIGHT, padx=5)

    def show_about(self):
        """Показать информацию о программе"""
        messagebox.showinfo(
            "О программе",
            "Система видеонаблюдения RTSP\n"
            "Версия 3.0\n\n"
            "Поддержка камер Dahua и других RTSP камер\n"
            "Все камеры отображаются в единой сетке\n"
            "Автоматическое переподключение при обрыве"
        )

    def on_closing(self):
        """Обработка закрытия приложения"""
        # Останавливаем все потоки
        for feed in self.feeds.values():
            feed.stop()

        self.root.destroy()

    def run(self):
        """Запуск приложения"""
        self.root.mainloop()


if __name__ == "__main__":
    app = CameraGridApp()
    app.run()