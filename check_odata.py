import requests
from requests.auth import HTTPBasicAuth
import urllib3
urllib3.disable_warnings()

url = "https://v12.arenda-it.com/efd8bqtl/ru_RU/odata/standard.odata/"

variants = [
    ("Администратор", ""),
    ("Администратор", "Admin12345"),
]

for login, password in variants:
    print(f"\n--- Пробую: {login} / {password or '(пусто)'} ---")
    try:
        r = requests.get(url, auth=HTTPBasicAuth(login, password), verify=False, timeout=10)
        print(f"Код ответа: {r.status_code}")
        if r.status_code == 200:
            print("✅ УСПЕХ! Вот фрагмент ответа:")
            print(r.text[:500])
            break
        else:
            print(f"Ответ: {r.text[:200]}")
    except Exception as e:
        print(f"Ошибка: {e}")
