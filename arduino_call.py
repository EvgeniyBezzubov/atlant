import requests

# Отправка запроса
response = requests.get('http://192.168.0.170')

# Проверка статуса (200 - OK)
print(response.status_code)

# Получение текста ответа

Uon1stAkkumIdStart = response.text.find("startU1")
Uon1stAkkumIdEnd = response.text.find("endU1")


print(response.text[Uon1stAkkumIdStart+7:Uon1stAkkumIdEnd])
