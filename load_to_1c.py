import requests
import openpyxl
import json
import sys
import urllib3
from requests.auth import HTTPBasicAuth

urllib3.disable_warnings()

# --- НАСТРОЙКИ ---
ODATA_URL = "https://v12.arenda-it.com/efd8bqtl/ru_RU/odata/standard.odata"
LOGIN = "api_user"
PASSWORD = "ApiPass123"
EXCEL_FILE = "catalog.xlsx"

def load_products():
    print(f"📂 Открываю {EXCEL_FILE}...")
    try:
        wb = openpyxl.load_workbook(EXCEL_FILE, data_only=True)
        sheet = wb.active
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return

    rows = list(sheet.iter_rows(min_row=2, values_only=True))
    total = len(rows)
    print(f"📊 Найдено строк: {total}")

    session = requests.Session()
    session.auth = HTTPBasicAuth(LOGIN, PASSWORD)
    session.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json"
    })

    # Проверка соединения
    try:
        print("🔌 Проверяю соединение с 1С...")
        res = session.get(f"{ODATA_URL}/$metadata", verify=False)
        if res.status_code != 200:
            print(f"❌ Ошибка соединения: {res.status_code} - {res.text[:200]}")
            return
        print("✅ Соединение установлено!")
    except Exception as e:
        print(f"❌ Ошибка сети: {e}")
        return

    created = 0
    errors = 0

    for i, row in enumerate(rows, start=2):
        sku = str(row[1]).strip() if row[1] else None
        name = str(row[4]).strip() if row[4] else None

        if not sku or not name:
            continue

        if sku.endswith('.0'):
            sku = sku[:-2]

        # Данные для 1С: Наименование (Description) и Артикул (Code)
        data = {
            "Description": name,
            "Code": sku,
            "IsFolder": False,
        }

        try:
            # Отправляем POST запрос на создание Номенклатуры
            res = session.post(f"{ODATA_URL}/Catalog_Номенклатура?$format=json", data=json.dumps(data), verify=False)
            
            if res.status_code == 201:
                created += 1
            else:
                # Если товар уже есть, 1С вернет 400 (дубликат). Пропускаем.
                if res.status_code == 400:
                    pass 
                else:
                    if errors < 5: # Показываем только первые 5 ошибок, чтобы не засорять консоль
                        print(f"⚠️ Строка {i} ({sku}): {res.status_code} - {res.text[:150]}")
                    errors += 1
        except Exception as e:
            if errors < 5:
                print(f"⚠️ Строка {i} ({sku}): {e}")
            errors += 1

        if i % 100 == 0:
            print(f"  ... обработано {i} из {total} (создано: {created}, ошибок: {errors})")

    print(f"\n✅ Готово!")
    print(f"   - Создано товаров: {created}")
    print(f"   - Ошибок/дубликатов: {errors}")

if __name__ == "__main__":
    load_products()
