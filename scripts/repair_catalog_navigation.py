"""Прикрепляет старые группы импорта по первому слову к шести разделам витрины.
Без --apply только отчёт. Товары, цены и остатки не меняет.
"""
import argparse,json
from pathlib import Path
from sqlalchemy import select,func
from app.database import SessionLocal
from app.models import Category,Product,Stock

GROUPS={
 'razdel-zapchasti-telefony':'Аккумулятор Аккумуляторы Антенна Вибромотор Винт Винты Динамик Дисплей Дисплейный Задняя Звонок Камера Кольцо Комплект Коннектор Контейнер Корпус Крышка Микросхемаконтроллер Направляющие Пленка Разъем Рамка Сетка Скотч Средняя Стекло Тачскрин Шлейф Шлейфплата Ячейка'.split(),
 'razdel-aksessuary':'Кабель Автомобильное Беспроводное Беспроводные Внешний Гарнитура Держатель Колонка Левый Наушники Переходник Правый Сетевое Чехол'.split(),
 'razdel-pk-noutbuki-tv-konsoli':['Блок','Матрица'],
 'razdel-zashchita-i-pamyat':['Гидрогелевая','Защитное'],
 'razdel-oborudovanie-instrumenty':'Жидкость Гель Изопропанол Клей Набор Органайзер Растворитель Спрейохладитель Спрейочиститель Спрейпылеудалитель'.split(),
}
NAMES={'аккумулятор':'Аккумуляторы для телефонов','аккумуляторы':'Комплекты аккумуляторов','дисплей':'Дисплеи','дисплейный':'Дисплейные модули','задняя':'Задние крышки','средняя':'Средние части','шлейф':'Шлейфы','шлейфплата':'Шлейфы и платы','защитное':'Защитные стёкла','гидрогелевая':'Гидрогелевые плёнки','блок':'Блоки питания для ноутбуков','матрица':'Матрицы','автомобильное':'Автомобильные зарядные устройства','внешний':'Внешние аккумуляторы','сетевое':'Сетевые зарядные устройства','пленка':'Плёнки OCA','левый':'Левые наушники','правый':'Правые наушники'}

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
 mapping={name.casefold():parent for parent,names in GROUPS.items() for name in names}
 with SessionLocal() as d:
  cats=list(d.scalars(select(Category)));by_slug={c.slug:c for c in cats}
  backup=[dict(id=c.id,name=c.name,parent_id=c.parent_id) for c in cats]
  total=d.scalar(select(func.sum(Stock.quantity)))
  changed=[]
  for c in cats:
   if c.slug not in mapping:continue
   parent=by_slug[mapping[c.slug]]
   if c.parent_id is None or c.parent_id==parent.id:
    c.parent_id=parent.id;c.name=NAMES.get(c.slug,c.name);changed.append(c.slug)
  print('legacy_groups_connected',len(changed))
  if args.apply:
   path=Path('var/backups/catalog-navigation-before.json')
   if not path.exists():path.write_text(json.dumps(backup,ensure_ascii=False,indent=2))
   d.commit()
   assert d.scalar(select(func.sum(Stock.quantity)))==total
   print('APPLIED; stock unchanged',total)
  else:d.rollback()
if __name__=='__main__':main()
