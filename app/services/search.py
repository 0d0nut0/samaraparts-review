"""
Поиск товаров по ключевым словам (витрина, подсказки, приход/списание в админке).

Запрос разбивается на слова, каждое слово должно найтись в названии или артикуле (И):
- латиница и цифры ищутся как целое слово: «11» не находит «110», «c67» находит «C53/C67»;
- русские слова сводятся к основе («рамке» → «рамк») и расширяются синонимами («акб» → «аккумулятор»);
- предлоги («в», «для», «с» …) отбрасываются.
Ранжирование: точный артикул → артикул с префикса → название начинается с типа детали →
меньше «лишних» модификаторов модели (Pro, Max, Plus… которых нет в запросе) → короче название.

Пример: «iPhone 11 дисплей», «realme c67 акб», «honor 10i дисплей в рамке», «iPhone 13 pro max шлейф», «BTT-PMIG110-HC».
"""
import re

from sqlalchemy import and_, or_, case, exists, func, literal
from sqlalchemy.sql.elements import ColumnElement

from app.models import MobaPrice, Product, Stock

STOPWORDS = {"в", "во", "на", "для", "с", "со", "и", "из", "по", "к", "от", "под", "без"}

# Синонимы: основа слова в запросе → основы, которые ищем в названиях
SYNONYMS = {
    "акб": ["аккумулятор", "акб", "батаре"],
    "аккум": ["аккумулятор", "акб"],
    "батар": ["батаре", "аккумулятор"],
    "диспл": ["диспле", "экран"],
    "экран": ["диспле", "экран"],
    "модул": ["диспле", "модул"],
    "тач": ["тачскрин"],
    "тачскрин": ["тачскрин"],
    "крышк": ["крышк"],
    "корпус": ["корпус", "крышк"],
    "зарядк": ["заряд"],
    "заряд": ["заряд"],
    "разъем": ["разъем", "разъём"],
    "разъём": ["разъем", "разъём"],
    "стекл": ["стекл"],
    "шлейф": ["шлейф"],
    "камер": ["камер"],
    "динамик": ["динамик"],
    "рамк": ["рамк"],
    "наушник": ["наушник", "гарнитур"],
    "пауэрбанк": ["power bank", "внешн"],
    # бренды кириллицей
    "айфон": ["iphone"],
    "айпад": ["ipad"],
    "самсунг": ["samsung"],
    "ксиоми": ["xiaomi"],
    "сяоми": ["xiaomi"],
    "редми": ["redmi"],
    "хонор": ["honor"],
    "хуавей": ["huawei"],
    "реалми": ["realme"],
    "техно": ["tecno"],
    "инфиникс": ["infinix"],
    "поко": ["poco"],
}

# Модификаторы модели: если их нет в запросе, такие товары опускаются ниже
QUALIFIERS = ("pro", "max", "plus", "mini", "lite", "ultra", "se", "fe", "prime", "neo", "air")

ENDINGS = sorted(
    ("ами", "ями", "ого", "ему", "ыми", "ими", "ах", "ях", "ов", "ев", "ей", "ой", "ый", "ий", "ая", "яя",
     "ое", "ее", "ые", "ие", "ом", "ем", "ам", "ям", "а", "я", "ы", "и", "е", "у", "ю", "о", "ь"),
    key=len, reverse=True,
)

TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
ARTICLE_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z\-_.]{2,}$")


def _stem(word: str) -> str:
    if len(word) <= 4:
        return word
    for e in ENDINGS:
        if word.endswith(e) and len(word) - len(e) >= 3:
            return word[: -len(e)]
    return word


def parse(query: str) -> dict:
    q = (query or "").strip()[:100]
    words = [w.lower().replace("ё", "е") for w in TOKEN_RE.findall(q)][:8]
    latin, cyr = [], []
    for w in words:
        if w in STOPWORDS:
            continue
        if re.fullmatch(r"[0-9a-z]+", w):
            latin.append(w)
        else:
            cyr.append(_stem(w))
    return {"raw": q, "latin": latin, "cyr": cyr, "article": q if ARTICLE_RE.match(q) and " " not in q else None}


def _cyr_condition(stem: str) -> ColumnElement:
    variants = SYNONYMS.get(stem) or next((v for k, v in SYNONYMS.items() if stem.startswith(k)), None) or [stem]
    conds = []
    for v in variants:
        if re.fullmatch(r"[0-9a-z ]+", v):
            conds.append(MobaPrice.name.op("~*")(r"\m" + re.escape(v).replace(r"\ ", " ") + r"\M"))
        else:
            conds.append(MobaPrice.name.ilike(f"%{v}%"))
    return or_(*conds)


def conditions(query: str) -> list[ColumnElement]:
    """Условия WHERE для MobaPrice. Пустой список — запрос пустой."""
    p = parse(query)
    conds = []
    for w in p["latin"]:
        conds.append(or_(MobaPrice.name.op("~*")(r"\m" + w + r"\M"), MobaPrice.sku.ilike(f"%{w}%")))
    for s in p["cyr"]:
        conds.append(_cyr_condition(s))
    if p["article"]:
        by_article = MobaPrice.sku.ilike(f"{p['article']}%")
        return [or_(by_article, and_(*conds))] if conds else [by_article]
    return conds


def score(query: str) -> ColumnElement:
    """Выражение релевантности (больше — выше в выдаче)."""
    p = parse(query)
    parts = [literal(0)]
    if p["article"]:
        parts.append(case((func.upper(MobaPrice.sku) == p["article"].upper(), 1000), else_=0))
        parts.append(case((MobaPrice.sku.ilike(f"{p['article']}%"), 500), else_=0))
    for i, s in enumerate(p["cyr"]):
        # «Дисплей для …» выше, чем «Наклейка на дисплей для …»; первое слово-тип в запросе главнее
        # («дисплей в рамке» — это дисплей, а не рамка)
        variants = SYNONYMS.get(s) or [s]
        parts.append(case((or_(*(MobaPrice.name.ilike(f"{v}%") for v in variants)), 40 if i == 0 else 15), else_=0))
    if len(p["latin"]) > 1:
        phrase = r"\m" + r"[\s/]+".join(p["latin"]) + r"\M"
        parts.append(case((MobaPrice.name.op("~*")(phrase), 30), else_=0))
    for qual in QUALIFIERS:
        if qual not in p["latin"]:
            parts.append(case((MobaPrice.name.op("~*")(r"\m" + qual + r"\M"), -25), else_=0))
    # Сначала то, что можно купить сейчас: с ценой и в наличии (иначе вверху «Цена по запросу» и «Нет в наличии»)
    parts.append(availability())
    expr = parts[0]
    for part in parts[1:]:
        expr = expr + part
    return expr


def availability() -> ColumnElement:
    """+300 — есть цена, +200 — есть на складе в любом городе."""
    in_stock = exists().where(Product.sku == MobaPrice.sku, Stock.product_id == Product.id, Stock.quantity > 0)
    return case((MobaPrice.our_base.isnot(None), 300), else_=0) + case((in_stock, 200), else_=0)


def order_by(query: str) -> list:
    return [score(query).desc(), func.length(MobaPrice.name), MobaPrice.id]
