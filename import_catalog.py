import openpyxl
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from dotenv import load_dotenv
import os
import re

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

from app.models.product import Product
from app.models.category import Category
from app.models.stock import Stock
from app.models.city import City

def get_or_create_category(db, name):
    # Убираем лишние пробелы
    name = name.strip()
    if not name:
        name = "Разное"
    
    # Ищем категорию
    cat = db.execute(select(Category).where(Category.name == name)).scalar_one_or_none()
    if not cat:
        # Создаем новую (slug делаем транслитом или просто name)
        cat = Category(name=name, slug=name.lower().replace(" ", "-"))
        db.add(cat)
        db.flush() # чтобы получить id
    return cat

def import_catalog():
    print("📂 Открываю catalog.xlsx...")
    try:
        wb = openpyxl.load_workbook("catalog.xlsx", data_only=True)
        sheet = wb.active
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return

    engine = create_engine(DATABASE_URL)
    db = Session(engine)

    created = 0
    skipped = 0
    categories_created = 0

    rows = list(sheet.iter_rows(min_row=2, values_only=True))
    total = len(rows)
    print(f"📊 Найдено строк: {total}")

    for i, row in enumerate(rows, start=2):
        # В catalog.xlsx штрихкод в колонке A, название в D, model в H, quality в I
        # Но судя по файлу, колонки: №, Штрихкод, ..., Наименование, ..., Model, Quality
        # Берем индексы: 1 - Штрихкод, 4 - Наименование (рабочее), 7 - Model, 8 - Quality
        sku_raw = row[1] 
        name = row[4]
        model = row[7]
        quality = row[8]

        if not sku_raw or not name:
            continue

        sku = str(sku_raw).strip()
        if sku.endswith('.0'):
            sku = sku[:-2]

        # Проверяем, есть ли уже такой товар
        existing = db.execute(select(Product).where(Product.sku == sku)).scalar_one_or_none()
        if existing:
            skipped += 1
            continue

        # Определяем категорию по первому слову в названии
        # Например: "Аккумулятор для Apple..." -> "Аккумулятор"
        first_word = str(name).split()[0] if name else "Разное"
        # Убираем знаки препинания
        first_word = re.sub(r'[^\w\s]', '', first_word)
        
        category = get_or_create_category(db, first_word)
        if category.id is None: # если только что создали
            categories_created += 1

        # Создаем товар
        product = Product(
            sku=sku,
            name=str(name),
            slug=str(name).lower().replace(" ", "-")[:500],
            category_id=category.id,
            model=str(model) if model else None,
            # quality мы не храним в Product, если надо - добавь поле
            is_active=1
        )
        db.add(product)
        created += 1

        if i % 500 == 0:
            print(f"  ... обработано {i} из {total}")
            db.commit() # сохраняем пачками

    db.commit()
    print(f"\n✅ Каталог загружен!")
    print(f"   - Создано товаров: {created}")
    print(f"   - Пропущено (уже есть): {skipped}")
    print(f"   - Создано категорий: {categories_created}")
    db.close()

if __name__ == "__main__":
    import_catalog()
