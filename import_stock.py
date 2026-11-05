import openpyxl
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from dotenv import load_dotenv
import os

# Загружаем настройки из .env
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("❌ Не найден DATABASE_URL в .env")
    exit(1)

# Импортируем модели
from app.models.product import Product
from app.models.stock import Stock
from app.models.city import City

def import_stock():
    print(f"📂 Открываю файл: stock.xlsx")
    try:
        wb = openpyxl.load_workbook("stock.xlsx", data_only=True)
        sheet = wb.active
    except FileNotFoundError:
        print("❌ Файл stock.xlsx не найден в папке проекта.")
        return
    except Exception as e:
        print(f"❌ Ошибка при открытии файла: {e}")
        return

    # Подключаемся к базе
    engine = create_engine(DATABASE_URL)
    db = Session(engine)

    # Находим город Казань (или создаем, если нет)
    city = db.execute(select(City).where(City.name.ilike("%Казань%"))).scalar_one_or_none()
    if not city:
        print("❌ Город 'Казань' не найден в таблице cities.")
        print("   Создай его в админке или дай команду, я добавлю.")
        db.close()
        return
    print(f"✅ Город: {city.name} (id={city.id})")

    updated = 0
    skipped = 0
    not_found = []

    # Читаем строки (пропускаем заголовок)
    rows = list(sheet.iter_rows(min_row=2, values_only=True))
    total = len(rows)
    print(f"📊 Найдено строк: {total}")

    for i, row in enumerate(rows, start=2):
        sku_raw = row[0]
        name = row[1]
        qty_raw = row[4]

        if not sku_raw or qty_raw is None:
            continue

        # Приводим штрихкод к строке (Excel может сохранить как число)
        sku = str(sku_raw).strip()
        if sku.endswith('.0'):
            sku = sku[:-2]

        try:
            qty = int(qty_raw)
        except (ValueError, TypeError):
            print(f"⚠️ Строка {i}: неверное количество '{qty_raw}' для {sku}. Пропускаю.")
            skipped += 1
            continue

        # Ищем товар по штрихкоду
        product = db.execute(select(Product).where(Product.sku == sku)).scalar_one_or_none()

        if product:
            # Ищем запись об остатке для этого товара в Казани
            stock = db.execute(
                select(Stock).where(
                    Stock.product_id == product.id,
                    Stock.city_id == city.id
                )
            ).scalar_one_or_none()

            if stock:
                stock.quantity = qty
            else:
                # Если записи нет — создаем
                new_stock = Stock(product_id=product.id, city_id=city.id, quantity=qty)
                db.add(new_stock)

            updated += 1
            if i % 50 == 0:
                print(f"  ... обработано {i} из {total}")
        else:
            not_found.append(f"{sku} | {name}")
            skipped += 1

    try:
        db.commit()
        print("\n✅ Импорт завершен!")
        print(f"   - Обновлено/добавлено остатков: {updated}")
        print(f"   - Пропущено (нет товара в базе): {skipped}")

        if not_found:
            print(f"\n⚠️ Товары, которых нет в базе ({len(not_found)} шт.):")
            for item in not_found[:20]:
                print(f"   - {item}")
            if len(not_found) > 20:
                print(f"   ... и еще {len(not_found) - 20}")
    except Exception as e:
        db.rollback()
        print(f"\n❌ Ошибка при сохранении: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    import_stock()
