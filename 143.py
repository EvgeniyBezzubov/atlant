import tkinter as tk
from tkinter import simpledialog
import keyboard
import threading
from multiprocessing import Process
import time
from threading import Thread
import keyboard
import socket
import pygame
import requests
import sys

def lift(arg_lift):
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # создаем сокет клиента
    client.settimeout(3.0)
    client.connect((hostname, port2))  # подключаемся к серверу
    message = "lift " + str(arg_lift)
    # 1.12 1.20 1.19 1.13 1.27  1.04   шаг 0.07-0.08 мВ при полном напряжении 2,36
    client.send(message.encode())  # отправляем сообщение серверу
    data = client.recv(1024)  # получаем данные с сервера
def startUs(arg_us, pos):
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # создаем сокет клиента
    client.settimeout(3.0)
    client.connect((hostname, port))  # подключаемся к серверу

    message = "startUs" + str(arg_us) + " " + str(pos)
    print(message)
    # 1.12 1.20 1.19 1.13 1.27  1.04   шаг 0.07-0.08 мВ при полном напряжении 2,36
    client.send(message.encode())  # отправляем сообщение серверу
    data = client.recv(1024)  # получаем данные с сервера
    print(data)
def run_elevator_new(polozhenie):
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # запуск элеватора стоп
    client.settimeout(3.0)
    client.connect((hostname, port2))  #
    message = "elevator_2 " + str(polozhenie)
    client.send(message.encode())  #
    data = client.recv(1024)  #
    print("Server sent: ", data.decode())
def Start_filtr_ochistki():
    global filtrochistki_isOn

    while True:

        if filtrochistki_isOn:
            time.sleep(periodvkl)
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # создаем сокет клиента
            client.settimeout(3.0)
            client.connect((hostname, port))  # подключаемся к серверу
            message = "relle1pos 0"
            client.send(message.encode())  # отправляем сообщение серверу
            data = client.recv(1024)  # получаем данные с сервера

            time.sleep(timeon)
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # создаем сокет клиента
            client.settimeout(3.0)
            client.connect((hostname, port))  # подключаемся к серверу
            message2 = "relle1pos 1"
            client.send(message2.encode())  # отправляем сообщение серверу
            data2 = client.recv(1024)  # получаем данные с сервера
            time.sleep(1)


def call_arduino(text=""):
    # Отправка запроса
    # response = requests.get('http://192.168.0.170'+text)
    response = requests.get('http://37.9.243.135', timeout=2)
    # Получение текста ответа
    Uon1stAkkumIdStart = response.text.find("Voltage")
    Uon1stAkkumIdEnd = response.text.find("endU1")
    string_value = response.text[Uon1stAkkumIdStart + 9:Uon1stAkkumIdStart + 14]
    cleaned_string = string_value.strip()  # удаляем пробелы в начале и конце
    number = float(cleaned_string) * 1.027
    return number
    # print("Напряжение питания: " + str(number))


def Wake_On_Lan():
    global gear2
    global gear
    global back
    global back2
    while True:
        # print("wakeUP")
        time.sleep(1)
        slowf_wake_UP = wake_UP
        slowf_wake_UP_Tread = Thread(target=slowf_wake_UP, args=(()))
        slowf_wake_UP_Tread.start()


def wake_UP():
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # создаем сокет клиента
        client.settimeout(1.0)
        client.connect((hostname, port))  # подключаемся к серверу
        message = "ONLINE"
        # 1.12 1.20 1.19 1.13 1.27  1.04   шаг 0.07-0.08 мВ при полном напряжении 2,36
        client.send(message.encode())  # отправляем сообщение серверу
        data = client.recv(1024)  # получаем данные с сервера
    #   print("Server sent: ", data.decode())
    except:
        print("Расбери 1 офлайн")
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # создаем сокет клиента
        client.settimeout(1.0)
        client.connect((hostname, port2))  # подключаемся к серверу
        message = "ONLINE"
        # 1.12 1.20 1.19 1.13 1.27  1.04   шаг 0.07-0.08 мВ при полном напряжении 2,36
        client.send(message.encode())  # отправляем сообщение серверу
        data = client.recv(1024)  # получаем данные с сервера
    except:
        print("Расбери 2 офлайн")


def revers2_left(revers_gear2):
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # создаем сокет клиента
        client.settimeout(3.0)
        client.connect((hostname, port))  # подключаемся к серверу
        message = "revers2 " + str(revers_gear2)
        client.send(message.encode())  # отправляем сообщение серверу
        data = client.recv(1024)  # получаем данные с сервера
    except:
        print("Попытка Установки реверса 1 неудачна")


def revers1_right(revers_gear1):
    try:

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # создаем сокет клиента
        client.settimeout(3.0)
        client.connect((hostname, port))  # подключаемся к серверу
        message = "revers1 " + str(revers_gear1)
        client.send(message.encode())  # отправляем сообщение серверу
        data = client.recv(1024)  # получаем данные с сервера
    except:
        print("Попытка Установки реверса 1 неудачна")


def swapgear_1engine_right(arg):
    global gear
    gear = int(arg)
    if gear <= 0:
        gear = -1 * gear + 2
        revers_gear = 1
    else:
        gear = gear + 2
        revers_gear = -1
    slow_revers1 = revers1_right
    slowfTread_revers1 = Thread(target=slow_revers1, args=(revers_gear,))
    slowfTread_revers1.start()

    # slow_revers2 = revers2
    # slowfTread_revers2 = Thread(target=slow_revers2, args=(revers_gear,))
    # slowfTread_revers2.start()

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # создаем сокет клиента
    client.settimeout(3.0)
    client.connect((hostname, port))  # подключаемся к серверу
    message = "start1gear" + str(gear)
    # 1.12 1.20 1.19 1.13 1.27  1.04   шаг 0.07-0.08 мВ при полном напряжении 2,36
    client.send(message.encode())  # отправляем сообщение серверу
    data = client.recv(1024)  # получаем данные с сервера
    #   print("Server sent: ", data.decode())


def swapgear_2engine_left(arg):
    gear = int(arg)
    if gear <= 0:
        gear = -1 * gear + 2
        revers_gear = 1
    else:
        gear = gear + 2
        revers_gear = -1

    slow_revers2 = revers2_left
    slowfTread_revers2 = Thread(target=slow_revers2, args=(revers_gear,))
    slowfTread_revers2.start()

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # создаем сокет клиента
    client.settimeout(3.0)
    client.connect((hostname, port))  # подключаемся к серверу
    message = "start2gear" + str(gear)
    # 1.12 1.20 1.19 1.13 1.27  1.04   шаг 0.07-0.08 мВ при полном напряжении 2,36
    client.send(message.encode())  # отправляем сообщение серверу
    data = client.recv(1024)  # получаем данные с сервера
    #   print("Server sent: ", data.decode())


def create_squares():
    # Создаём окно
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes('-topmost', True)
    root.attributes('-alpha', 0.85)  # Прозрачность 85%

    # Параметры
    square_size = 40
    num_squares = 5
    spacing = 10  # Расстояние между блоками

    # Дополнительные параметры для дополнительных элементов
    circle_size = 25
    circle_spacing = 45  # Расстояние между кружками по вертикали

    # Состояния элементов (4 состояния для элеватора: 0-серый1, 1-зелёный, 2-серый2, 3-красный)
    elevator_level = 0  # 0 - серый, 1 - зелёный, 2 - серый, 3 - красный
    mustache_level = 0  # 0 - красный, 1 - зелёный
    lift_level = 0  # 0 - красный, 1 - зелёный
    pump_level = 0  # 0 - красный, 1 - зелёный
    filter_level = 0  # 0 - красный, 1 - зелёный

    # Параметры фильтра
    filter_interval = ""  # Интервал включения
    filter_period = ""  # Период включения

    # Параметры напряжения сети
    network_voltage = 26  # Напряжение сети (захардкожено 26)

    # Уровни для левого и правого блока
    left_level = 0  # -2, -1, 0, 1, 2, 3
    right_level = 0  # -2, -1, 0, 1, 2, 3

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
            if level == 0 or level == 2:
                color = "gray"
            elif level == 1:
                color = "green"
            elif level == 3:
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

        elevator_status = ['серый', 'зелёный', 'серый', 'красный'][elevator_level]
        mustache_status = ['красный', 'зелёный'][mustache_level]
        lift_status = ['красный', 'зелёный'][lift_level]
        pump_status = ['красный', 'зелёный'][pump_level]
        filter_status = ['красный', 'зелёный'][filter_level]

        print(f"Элеватор: {elevator_status} | Усы: {mustache_status} | "
              f"Подъёмники: {lift_status} | Помпа: {pump_status} | "
              f"Фильтр: {filter_status} [{filter_interval} {filter_period}]")

    def update_squares():
        # Обновляем левый блок
        for i in range(num_squares):
            color = "gray"

            if left_level == -1:
                if i == 1:
                    color = "red"
            elif left_level == -2:
                if i == 0 or i == 1:
                    color = "red"
            elif left_level == 1:
                if i == 2:
                    color = "green"
            elif left_level == 2:
                if i == 2 or i == 3:
                    color = "green"
            elif left_level == 3:
                if i == 2 or i == 3 or i == 4:
                    color = "green"

            canvas.itemconfig(left_shapes[i], fill=color)

        # Обновляем правый блок
        for i in range(num_squares):
            color = "gray"

            if right_level == -1:
                if i == 1:
                    color = "red"
            elif right_level == -2:
                if i == 0 or i == 1:
                    color = "red"
            elif right_level == 1:
                if i == 2:
                    color = "green"
            elif right_level == 2:
                if i == 2 or i == 3:
                    color = "green"
            elif right_level == 3:
                if i == 2 or i == 3 or i == 4:
                    color = "green"

            canvas.itemconfig(right_shapes[i], fill=color)
        slow_swapgear_1engine_right = swapgear_1engine_right
        slowfTread_swapgear_1engine = Thread(target=slow_swapgear_1engine_right, args=(right_level,))
        slowfTread_swapgear_1engine.start()

        slow_swapgear_2engine_left = swapgear_2engine_left
        slowfTread_swapgear_2engine = Thread(target=slow_swapgear_2engine_left, args=(left_level,))
        slowfTread_swapgear_2engine.start()

        print(f"Левый блок: уровень {left_level} | Правый блок: уровень {right_level}")

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
        if left_level > -2:
            left_level -= 1
        if right_level > -2:
            right_level -= 1
        update_squares()

    def on_left():
        nonlocal left_level, right_level
        if right_level < 3:
            right_level += 1
        if left_level > -2:
            left_level -= 1
        update_squares()

    def on_right():
        nonlocal left_level, right_level
        if right_level > -2:
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
        # 4 состояния: 0 -> 1 -> 2 -> 3 -> 0
        elevator_level = (elevator_level + 1) % 4
        update_circle(0, elevator_level, "4state")

        slow_run_elevator_new = run_elevator_new
        slowfTread_slow_run_elevator_new = Thread(target=slow_run_elevator_new, args=(elevator_level,))
        slowfTread_slow_run_elevator_new.start()

        elevator_status = ['серый', 'зелёный', 'серый', 'красный'][elevator_level]
        print(f"Элеватор: {elevator_status}")

    def on_mustache_key():
        nonlocal mustache_level
        mustache_level = (mustache_level + 1) % 2

        startUs("3", mustache_level)
        startUs("4", mustache_level)

        update_circle(1, mustache_level, "2state")
        print(f"Усы: {['красный', 'зелёный'][mustache_level]}")

    def on_lift_key():
        nonlocal lift_level
        lift_level = (lift_level + 1) % 2
        if lift_level == 0:
            lift(str("-1"))
        else:
            lift(str("1"))
        update_circle(2, lift_level, "2state")
        print(f"Подъёмники: {['красный', 'зелёный'][lift_level]}")

    def on_pump_key():
        nonlocal pump_level
        pump_level = (pump_level + 1) % 2
        startUs("1", pump_level)
        update_circle(3, pump_level, "2state")
        print(f"Помпа: {['красный', 'зелёный'][pump_level]}")

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

    # Регистрируем горячие клавиши
    keyboard.add_hotkey("up", on_up)
    keyboard.add_hotkey("down", on_down)
    keyboard.add_hotkey("right", on_right)
    keyboard.add_hotkey("left", on_left)
    keyboard.add_hotkey("space", on_space)
    keyboard.add_hotkey("e", on_elevator_key)
    keyboard.add_hotkey("y", on_mustache_key)
    keyboard.add_hotkey("u", on_lift_key)
    keyboard.add_hotkey("p", on_pump_key)
    keyboard.add_hotkey("f", on_filter_key)
    keyboard.add_hotkey("n", on_network_voltage_key)
    keyboard.add_hotkey("esc", on_esc)

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
    print('🟡 "E" - переключение ЭЛЕВАТОРА (серый→зелёный→серый→красный)')
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


hostname = "37.9.243.135"  # получаем хост локальной машины
hostname2 = "192.168.0.169"

port = 12345  # устанавливаем порт сервера
port2 = 12346

filtrochistki_isOn = True
periodvkl = 120
timeon = 0.5
if __name__ == "__main__":
    Wake_On_Lan_obj = Wake_On_Lan
    Thread_Wake_On_Lan_obj = Thread(target=Wake_On_Lan_obj, args=())
    Thread_Wake_On_Lan_obj.start()

    create_squares_obj = create_squares
    Thread_create_squares_obj = Thread(target=create_squares_obj, args=())
    Thread_create_squares_obj.start()

    create_Start_filtr_ochistki = Start_filtr_ochistki
    Thread_create_Start_filtr_ochistki = Thread(target=create_Start_filtr_ochistki, args=())
    Thread_create_Start_filtr_ochistki.start()