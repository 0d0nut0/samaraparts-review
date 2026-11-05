"""Сопоставление референсных цен и оценка наценки по подтверждённым закупкам.

Из названий не удаляются модели, цвета, ёмкости, комплектация и качество.
Нечёткие совпадения не назначают цену автоматически.
"""
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from app.services.pricing import round_money


def normalize_name(name: str) -> str:
    text = unicodedata.normalize('NFKC', name or '').casefold().replace('ё', 'е')
    text = re.sub(r'\bapple\s+(?=iphone|ipad|ipod|airpods|watch)', '', text)
    text = re.sub(r'\bsamsung\s+galaxy\b', 'samsung', text)
    text = re.sub(r'\bor\s*100\s*%?', 'or100', text)
    # A plus sign is part of the model (S10+ and S10 are different parts).
    text = text.replace('+', ' plus ')
    text = re.sub(r'(?<=\d)plus\b', ' plus', text)
    aliases = {'акб': 'аккумулятор', 'premium': 'премиум', 'прем': 'премиум',
               'ориг': 'or', 'оригинал': 'or', 'orig': 'or', 'original': 'or',
               'черн': 'черный', 'бел': 'белый'}
    tokens = re.findall(r'[a-zа-я0-9]+', text)
    return ' '.join(aliases.get(token, token) for token in tokens if token != 'для')


def identity_tokens(name: str) -> tuple:
    # Word order is immaterial only when every token (including repetitions)
    # matches. In particular, "без рамки" never becomes "с рамкой".
    return tuple(sorted(normalize_name(name).split()))


def display_identity_tokens(name: str, *, omit_added_codes=False) -> tuple | None:
    """Старое название Moba из Excel → обновлённое описание дисплея.

    Только явные синонимы комплектации и отдельные скобки с разрешением,
    яркостью и частотой. OLED/In-Cell, качество, цвет, IC и модели сохраняются.
    Добавленный код модели можно опустить только на стороне Moba, причём
    неоднозначный результат никогда не выбирается автоматически.
    """
    text = (name or '').casefold().replace('ё', 'е')
    if not text.startswith('дисплей '):
        return None

    def bracket(match):
        value = match[1]
        if (omit_added_codes and re.fullmatch(r'[a-z0-9/ .-]+', value)
                and re.search(r'\d{3}', value) and re.search('[a-z]', value)):
            return ''
        technical = r'\s*(?:(?:full\s+)?(?:hd|fhd|qhd|cop|cof)\+?(?:\s+(?:cop|cof))?|\d+[.,]?\d*\s*(?:кд/м[²2]|гц))\s*'
        if all(re.fullmatch(technical, part) for part in value.split(',')):
            return ''
        return match[0]

    text = re.sub(r'\(([^()]*)\)', bracket, text)
    text = re.sub(r'\s*(?:в сборе с тачскрином|\+\s*тач(?:скрин)?)(?=\s|$)', ' безрамки ', text)
    text = re.sub(r'\s*модуль(?:\s+с\s+рамкой)?(?=\s|$)', ' срамкой ', text)
    return identity_tokens(text)


@dataclass(frozen=True)
class PriceMatch:
    product: dict | None
    reason: str
    candidates: tuple[str, ...] = ()


class PriceMatcher:
    def __init__(self, products: list[dict]):
        self.exact = defaultdict(dict)
        self.tokens = defaultdict(dict)
        self.displays = defaultdict(dict)
        self.displays_added_codes = defaultdict(dict)
        for product in products:
            article, name = product.get('article'), product.get('name')
            if not article or not name:
                continue
            self.exact[normalize_name(name)][article] = product
            self.tokens[identity_tokens(name)][article] = product
            key = display_identity_tokens(name)
            if key:
                self.displays[key][article] = product
                self.displays_added_codes[display_identity_tokens(name, omit_added_codes=True)][article] = product

    def match(self, source: dict) -> PriceMatch:
        # The explicit Moba-name column is a supplied mapping; full working
        # names precede abbreviated printed names for the other rows.
        for field in ('moba_name', 'name', 'print_name'):
            alias = source.get(field)
            if not alias:
                continue
            found = self.exact.get(normalize_name(alias), {})
            if not found:
                found = self.tokens.get(identity_tokens(alias), {})
            if len(found) == 1:
                return PriceMatch(next(iter(found.values())), field)
            if len(found) > 1:
                return PriceMatch(None, 'ambiguous', tuple(sorted(found)))
        # This fallback applies only to the explicitly supplied old Moba name.
        # Never strip a code explicitly provided by our catalogue.
        key = display_identity_tokens(source.get('moba_name', ''))
        found = self.displays.get(key) or self.displays_added_codes.get(key) or {}
        if len(found) == 1:
            return PriceMatch(next(iter(found.values())), 'moba_display_alias')
        if len(found) > 1:
            return PriceMatch(None, 'ambiguous', tuple(sorted(found)))
        return PriceMatch(None, 'not_found')


def product_group(name: str) -> str:
    text = normalize_name(name)
    for prefix, group in [('аккумулятор', 'battery'), ('ячейка', 'battery_cell'),
                          ('дисплей', 'display'), ('тачскрин', 'touchscreen'),
                          ('задняя крышка', 'back_cover'), ('корпус', 'housing'),
                          ('шлейф', 'flex'), ('звонок', 'buzzer'), ('динамик', 'speaker'),
                          ('камера', 'camera'), ('коннектор', 'connector'),
                          ('разъем', 'port'), ('кабель', 'cable'),
                          ('стереогарнитура', 'headset'), ('гарнитура', 'headset'),
                          ('наушники', 'headset')]:
        if text.startswith(prefix):
            return group
    if 'стекло' in text:
        return 'camera_glass' if 'камер' in text else 'glass'
    if 'зарядное' in text or text.startswith(('сзу', 'азу')):
        return 'charger'
    return 'other'


@dataclass(frozen=True)
class PricingPeer:
    cost: Decimal
    retail: Decimal
    group: str
    quality: str = ''


@dataclass(frozen=True)
class EstimatedPrice:
    retail: Decimal
    multiplier: Decimal
    sample_size: int
    scope: str


def estimate_price(cost: Decimal | None, group: str, quality: str,
                   peers: list[PricingPeer]) -> EstimatedPrice | None:
    """Медиана Moba-цена/наша-себестоимость по реальным совпавшим товарам.

    Приоритет: тот же тип, качество и близкая себестоимость. Оценки не служат
    обучающими примерами для других оценок. Без закупочной стоимости цены нет.
    """
    if cost is None or not cost.is_finite() or cost <= 0:
        return None
    valid = [p for p in peers if p.cost.is_finite() and p.retail.is_finite()
             and p.cost > 0 and p.retail > p.cost]
    close = lambda p: cost / 2 <= p.cost <= cost * 2
    same_group = lambda p: p.group == group and group != 'other'
    same_quality = lambda p: bool(quality) and normalize_name(p.quality) == normalize_name(quality)
    scopes = [
        ('type_quality_cost', [p for p in valid if same_group(p) and same_quality(p) and close(p)], 5),
        ('type_cost', [p for p in valid if same_group(p) and close(p)], 5),
        ('type', [p for p in valid if same_group(p)], 8),
        ('similar_cost', [p for p in valid if close(p)], 12),
        ('catalog', valid, 20),
    ]
    for scope, selected, minimum in scopes:
        if len(selected) >= minimum:
            factor = median(p.retail / p.cost for p in selected)
            return EstimatedPrice(round_money(cost * factor), factor, len(selected), scope)
    return None
