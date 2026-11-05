"""catalog sections tree and 1C exchange identifiers

Revision ID: c3a9f1d2e7b4
Revises: b7d2e4a91c3f
Create Date: 2026-10-26 18:00:00.000000

Строит дерево каталога из 6 разделов поверх плоских категорий moba.ru
(связь по slug) и добавляет поля для обмена с 1С (CommerceML).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3a9f1d2e7b4'
down_revision: Union[str, Sequence[str], None] = 'b7d2e4a91c3f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (slug, название, иконка, позиция)
SECTIONS = [
    ("razdel-zapchasti-telefony", "Запчасти для телефонов и планшетов", "📱", 1),
    ("razdel-aksessuary", "Аксессуары", "🎧", 2),
    ("razdel-pk-noutbuki-tv-konsoli", "Запчасти для ПК, ноутбуков, ТВ и консолей", "💻", 3),
    ("razdel-zashchita-i-pamyat", "Защита и память", "🛡️", 4),
    ("razdel-oborudovanie-instrumenty", "Оборудование и инструменты", "🛠️", 5),
    ("razdel-elementy-pitaniya", "Элементы питания", "🔋", 6),
]

# Родитель для существующих категорий: slug категории -> slug родителя
PARENTS = {
    # 1. Телефоны и планшеты
    "akkumulyatory-dlya-mobilnykh-ustroystv": "razdel-zapchasti-telefony",
    "yacheyki-banki-akkumulyatora": "razdel-zapchasti-telefony",
    "displei": "razdel-zapchasti-telefony",
    "zadnie-kryshki-i-korpusa": "razdel-zapchasti-telefony",
    "stekla-tachskriny-polyarizatory-polyarizatsionnye-i-osa-plenki": "razdel-zapchasti-telefony",
    "zapchasti": "razdel-zapchasti-telefony",
    "shleyfy-platy": "razdel-zapchasti-telefony",
    "korpusnye-chasti-ramki-skotch-tolkateli-i-t-p": "razdel-zapchasti-telefony",
    "mikroskhemy-kontrollery-usiliteli-i-t-p": "razdel-zapchasti-telefony",
    **{s: "stekla-tachskriny-polyarizatory-polyarizatsionnye-i-osa-plenki" for s in (
        "polyarizatory-displeya", "polyarizatsionnye-i-osa-plenki", "stekla-dlya-perekleyki", "tachskriny")},
    **{s: "zapchasti" for s in (
        "antennye-provoda", "vibromotory-i-magnity-besprovodnoy-zaryadki", "dinamiki-speaker", "zvonki-buzzer",
        "kamery", "knopki-vklyucheniya-gromkosti", "konnektory-sim-mmc-akb-lcd-fpc-i-t-p", "materinskie-platy",
        "mikrofony", "razemy-zaryadki-microusb-type-c-lightning-miniusb-i-t-p")},
    **{s: "korpusnye-chasti-ramki-skotch-tolkateli-i-t-p" for s in (
        "vinty", "izolyatsionnye-i-pylezashchitnye-nakleyki", "komplekty-metallicheskikh-plastin", "konteynery-sim",
        "ramki-srednie-chasti", "setki-dinamika", "skotchi-dvustoronnie-dlya-sborki-ustroystv", "stekla-kamery")},
    # 2. Аксессуары
    **{s: "razdel-aksessuary" for s in (
        "aksessuary", "besprovodnye-naushniki-bluetooth", "garnitury", "vneshnie-akkumulyatory-power-bank",
        "zaryadnye-ustroystva", "kabeli-adaptery", "chekhly", "derzhateli", "zashchitnye-zaglushki-i-setki",
        "prochie-aksessuary", "setevye-filtry-bloki-i-kabeli-pitaniya-bytovye", "klaviatury-myshi-geympady")},
    # 3. ПК, ноутбуки, ТВ, консоли
    **{s: "razdel-pk-noutbuki-tv-konsoli" for s in (
        "akkumulyatory", "bloki-i-kabeli-pitaniya", "videokabeli-perekhodniki-i-udliniteli", "dinamiki",
        "zhestkie-diski-i-komplektuyushchie", "klaviatury", "kryshki-matrits", "matritsy", "operativnaya-pamyat",
        "petli-dlya-matrits", "poddony-i-rezinovye-elementy-noutbukov", "razyomy",
        "svetodiodnye-podsvetki-i-svetodiody-dlya-remonta-televizorov", "sistemy-okhlazhdeniya",
        "trekpady-tachpady", "shleyfy", "zapchasti-dlya-igrovykh-pristavok")},
    # 4. Защита и память
    "zashchitnye-stekla-i-plenki": "razdel-zashchita-i-pamyat",
    "karty-pamyati": "razdel-zashchita-i-pamyat",
    "usb-flesh": "karty-pamyati",
    "karta-pamyati-microsd": "karty-pamyati",
    # 5. Оборудование и инструменты
    **{s: "razdel-oborudovanie-instrumenty" for s in (
        "oborudovanie-instrumenty-raskhodnye-materialy", "dlya-payki", "instrumenty",
        "oborudovanie-dlya-razdeleniya-displeynykh-moduley", "raskhodnye-materialy",
        "elektricheskoe-i-izmeritelnoe-oborudovanie")},
    # 6. Элементы питания
    **{s: "razdel-elementy-pitaniya" for s in (
        "batareyki", "akkumulyatory-bytovye", "akkumulyatory-universalnye", "elementy-pitaniya-i-komplektuyushchie-k-nim")},
}

# Защитные стекла и пленки — все подкатегории с префиксом slug
PROTECTION_PREFIXES = ("zashchitnye-stekla-", "zashchitnaya-plenka-", "zashchitnye-plenki-")

# Уточнённые названия (в moba одинаковые имена у разных групп)
RENAMES = {
    "akkumulyatory": "Аккумуляторы для ноутбуков",
    "aksessuary": "Аксессуары — разное",
    "oborudovanie-instrumenty-raskhodnye-materialy": "Оборудование — разное",
    "akkumulyatory-dlya-mobilnykh-ustroystv": "Аккумуляторы",
    "zapchasti": "Внутренние компоненты",
    "dinamiki": "Динамики для ноутбуков и ТВ",
    "shleyfy": "Шлейфы для ноутбуков и ТВ",
    "razyomy": "Разъёмы для ноутбуков и ТВ",
    "karty-pamyati": "Карты памяти и USB-накопители",
}


def upgrade() -> None:
    op.add_column('categories', sa.Column('position', sa.Integer(), nullable=False, server_default='100'))
    op.add_column('categories', sa.Column('icon', sa.String(length=16), nullable=True))
    op.add_column('categories', sa.Column('onec_id', sa.String(length=64), nullable=True))
    op.create_index('ix_categories_onec_id', 'categories', ['onec_id'], unique=True)
    op.create_index('ix_categories_parent_id', 'categories', ['parent_id'])
    op.add_column('products', sa.Column('onec_id', sa.String(length=80), nullable=True))
    op.create_index('ix_products_onec_id', 'products', ['onec_id'], unique=True)
    op.add_column('cities', sa.Column('onec_warehouse_id', sa.String(length=64), nullable=True))
    op.add_column('orders', sa.Column('onec_exported_at', sa.DateTime(), nullable=True))

    conn = op.get_bind()
    for slug, name, icon, pos in SECTIONS:
        conn.execute(
            sa.text(
                "INSERT INTO categories (name, slug, parent_id, position, icon) VALUES (:n, :s, NULL, :p, :i) "
                "ON CONFLICT (slug) DO UPDATE SET name = :n, position = :p, icon = :i"
            ),
            {"n": name, "s": slug, "p": pos, "i": icon},
        )
    for child, parent in PARENTS.items():
        conn.execute(
            sa.text("UPDATE categories SET parent_id = (SELECT id FROM categories WHERE slug = :p) WHERE slug = :c"),
            {"p": parent, "c": child},
        )
    for prefix in PROTECTION_PREFIXES:
        conn.execute(
            sa.text(
                "UPDATE categories SET parent_id = (SELECT id FROM categories WHERE slug = 'zashchitnye-stekla-i-plenki') "
                "WHERE slug LIKE :p AND slug <> 'zashchitnye-stekla-i-plenki'"
            ),
            {"p": prefix + "%"},
        )
    for slug, name in RENAMES.items():
        conn.execute(sa.text("UPDATE categories SET name = :n WHERE slug = :s"), {"n": name, "s": slug})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE categories SET parent_id = NULL"))
    conn.execute(sa.text("DELETE FROM categories WHERE slug LIKE 'razdel-%'"))
    op.drop_column('orders', 'onec_exported_at')
    op.drop_column('cities', 'onec_warehouse_id')
    op.drop_index('ix_products_onec_id', table_name='products')
    op.drop_column('products', 'onec_id')
    op.drop_index('ix_categories_parent_id', table_name='categories')
    op.drop_index('ix_categories_onec_id', table_name='categories')
    op.drop_column('categories', 'onec_id')
    op.drop_column('categories', 'icon')
    op.drop_column('categories', 'position')
