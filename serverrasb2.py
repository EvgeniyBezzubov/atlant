#!/usr/bin/env python
import socket
import RPi.GPIO as GPIO
import time
import threading
from _thread import *

# Глобальная переменная для отслеживания времени последней команды
last_command_time = time.time()
# Блокировка для безопасного доступа к глобальной переменной из разных потоков
command_time_lock = threading.Lock()
# Флаг для остановки потока мониторинга
monitoring_active = True


def update_last_command_time():
    """Обновляет время последней полученной команды"""
    global last_command_time
    with command_time_lock:
        last_command_time = time.time()


def monitor_inactivity():
    """Мониторит отсутствие команд и переключает пины в HIGH при бездействии"""
    global monitoring_active
    global last_command_time
    # Список всех используемых GPIO пинов
    all_gpio_pins = [GPIO20, GPIO21]

    while monitoring_active:
        time.sleep(1)  # Проверяем каждую секунду
        with command_time_lock:
            time_since_last_command = time.time() - last_command_time

        if time_since_last_command >= 5:
            # Переключаем все пины в HIGH
            for pin in all_gpio_pins:
                GPIO.output(pin, GPIO.HIGH)
            # Сбрасываем время, чтобы не повторять переключение постоянно
            with command_time_lock:
                last_command_time = time.time()
            print("No commands received for 5 seconds. All pins set to HIGH")


def client_thread(con):
    data = con.recv(1024)
    message = data.decode()
    datalist = message.split(" ")
    print(datalist)

    # Обновляем время при получении любой команды
    update_last_command_time()

    if datalist[0] == "elevator":
        elevator(int(datalist[1]))

    messageout = datalist[0][::-1]
    con.send(messageout.encode())
    con.close()


def elevator(arg):
    if arg == 0:
        GPIO.output(GPIO20, GPIO.HIGH)
        GPIO.output(GPIO21, GPIO.HIGH)
    elif arg == 1:
        GPIO.output(GPIO20, GPIO.LOW)
        GPIO.output(GPIO21, GPIO.HIGH)
    elif arg == 2:
        GPIO.output(GPIO20, GPIO.HIGH)
        GPIO.output(GPIO21, GPIO.HIGH)
    elif arg == 3:
        GPIO.output(GPIO20, GPIO.HIGH)
        GPIO.output(GPIO21, GPIO.LOW)


GPIO.setmode(GPIO.BCM)
GPIO20 = 20
GPIO21 = 21 
GPIO.setup(GPIO20, GPIO.OUT)
GPIO.output(GPIO20, GPIO.HIGH)

GPIO.setup(GPIO21, GPIO.OUT)
GPIO.output(GPIO21, GPIO.HIGH)

# Запускаем поток мониторинга бездействия
monitoring_thread = threading.Thread(target=monitor_inactivity, daemon=True)
monitoring_thread.start()

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
# hostname = "192.168.8.4"
hostname = "192.168.0.169"
print(hostname)
port = 12346
server.bind((hostname, port))
server.listen(5)

print("servet run")

while True:
    client, _ = server.accept()
    start_new_thread(client_thread, (client,))
