"""
Привязка фотографий к товарам по названию файла.

Файл «АКБ-iPhone-16-Pro-ориг.jpg» подходит товару «Аккумулятор для Apple iPhone 16 Pro …», если совпадают:
  - тип детали (аккумулятор, дисплей, шлейф, стекло, крышка, камера…),
  - номера модели (16, a51, n975f…) и модификаторы (pro, max, plus, mini…),
  - и общих слов не меньше половины.
Иначе фото не ставим: лучше без фото, чем с чужой деталью.

Фото уменьшаются до 900 px и сохраняются в media/products/ (раздаётся по /media/products/…).
Поддерживаются имена, испорченные распаковкой zip (UTF-8, прочитанный как CP866).

  python -m scripts.attach_photos "C:/…/СайтФотографии" [--dry-run]
"""
import argparse
import hashlib
import os
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from PIL import Image
from sqlalchemy import select

from app.database import SessionLocal
from app.models import MobaPrice, Product

MEDIA_DIR = Path(__file__).resolve().parent.parent / "media" / "products"
MEDIA_URL = "/media/products/"

# Тип детали — по первым словам названия (иначе «Скотч аккумулятора» станет аккумулятором)
TYPE_PREFIXES = [
    ("camglass", ("стекло кам", "стекла кам", "стекло-кам")),
    ("glass", ("защитное стекло", "3с", "зс", "3c", "защитная пленка")),
    ("battery", ("акб", "аккумулятор ", "аккумулятор для", "akб", "ak ")),
    ("display", ("дм ", "дм-", "дисплей", "дисплейный")),
    ("flex", ("шлейф",)),
    ("cover", ("задняя крышка", "корпус")),
    ("camera", ("камера",)),
    ("buzzer", ("звонок",)),
    ("board", ("плата",)),
]
# Для крышек и дисплеев важен цвет, для шлейфов — назначение (разъём, вспышка, кнопки…)
COLORS = {"черн": "black", "чёрн": "black", "бел": "white", "син": "blue", "голуб": "blue", "золот": "gold",
          "натурал": "natural", "титан": "natural", "зелен": "green", "красн": "red", "розов": "pink",
          "фиолет": "purple", "сер": "gray", "black": "black", "white": "white"}
FLEX_PARTS = {"сист": "port", "разъем": "port", "разьем": "port", "зар": "port", "вспыш": "flash",
              "кноп": "buttons", "громк": "buttons", "вкл": "buttons", "беспровод": "wireless", "сенс": "sensor",
              "межплат": "board2board", "антенн": "antenna", "камер": "camera", "динамик": "speaker"}


def details(text: str, table: dict) -> frozenset:
    words = re.findall(r"[a-zа-яё]+", text.lower())
    return frozenset(v for w in words for k, v in table.items() if w.startswith(k))


# Бренд на фото должен совпадать с брендом товара (REMAX на фото — только товарам REMAX)
BRANDS = {"remax", "kelai", "breaking", "hoco", "borofone", "baseus", "jcid"}
QUALIFIERS = {"pro", "max", "plus", "mini", "lite", "ultra", "se", "fe", "prime", "neo", "air"}
NOISE = {
    "or", "orig", "original", "prem", "premium", "oem", "oled", "incell", "in", "cell", "hc", "sp", "rj", "new",
    "mah", "foxconn", "full", "size", "fhd", "hd", "tft", "for", "apple", "galaxy", "with", "frame", "copy",
    "lcd", "jpg", "3d", "used", "diagnostic", "ios", "standart", "capacity", "high", "gx", "kelai", "ak",
}
# Кириллица, похожая на латиницу: «Рго» → «pro», «Хіаоті-Росо-Х3» → «xiaomi poco x3»
LOOKALIKE = str.maketrans("аеорсухкмнтвіАЕОРСУХКМНТВІ", "aeopcyxkmhtbiaeopcyxkmhtbi")
ALIASES = {"iph": "iphone", "ipone": "iphone", "xiaomi": "xiaomi", "pgo": "pro", "rpo": "pro"}


def fix_name(name: str) -> str:
    try:
        name = name.encode("cp866").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return unicodedata.normalize("NFC", name)


def features(text: str) -> tuple[str | None, frozenset, frozenset, set]:
    """(тип, номера модели, модификаторы, все значимые слова)."""
    text = text.lower().replace("ё", "е")
    words = re.findall(r"[0-9a-zа-яі+]+", text)
    head = re.sub(r"[-_]+", " ", text).strip() + " "
    kind = next((k for k, prefixes in TYPE_PREFIXES if head.startswith(prefixes)), None)
    tokens = set()
    for w in words:
        latin = w.translate(LOOKALIKE)
        if re.fullmatch(r"[0-9a-z+]+", latin):
            latin = ALIASES.get(latin.rstrip("+"), latin.rstrip("+"))
            if latin and latin not in NOISE and not (latin.isdigit() and int(latin) >= 1000):
                tokens.add(latin)
    digits = frozenset(t for t in tokens if re.search(r"\d", t) and t not in {"3c"})
    quals = frozenset(t for t in tokens if t in QUALIFIERS)
    return kind, digits, quals, tokens


def collect_photos(root: str) -> dict:
    index = defaultdict(list)
    for dirpath, _, files in os.walk(root):
        for f in files:
            if not f.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            path = os.path.join(dirpath, f)
            name = fix_name(os.path.splitext(f)[0])
            if "kelai" in fix_name(dirpath).lower():
                name = "ДМ " + name  # фото дисплеев KELAI
            kind, digits, quals, tokens = features(name)
            if kind and digits:
                index[(kind, digits, quals)].append((tokens, path, name))
    return index


def save_resized(src: str) -> str:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(src.encode("utf-8")).hexdigest()[:16] + ".jpg"
    target = MEDIA_DIR / name
    if not target.exists():
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((900, 900))
            im.save(target, "JPEG", quality=82, optimize=True)
    return MEDIA_URL + name


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("photos")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    index = collect_photos(args.photos)
    print(f"фото с распознанным типом и моделью: {sum(len(v) for v in index.values())}")
    matched, used = 0, set()
    with SessionLocal() as db:
        products = db.execute(select(Product)).scalars().all()
        mps = {m.sku: m for m in db.execute(select(MobaPrice)).scalars()}
        for p in products:
            kind, digits, quals, tokens = features(p.name)
            cands = index.get((kind, digits, quals)) if kind and digits else None
            if not cands:
                continue
            cands = [c for c in cands if (c[0] & BRANDS) <= tokens]
            if kind in ("cover", "display"):
                color = details(p.name, COLORS)
                cands = [c for c in cands if details(c[2], COLORS) == color]
            elif kind == "flex":
                part = details(p.name, FLEX_PARTS)
                cands = [c for c in cands if details(c[2], FLEX_PARTS) == part]
            if not cands:
                continue
            best = max(cands, key=lambda c: len(tokens & c[0]) / len(tokens | c[0]))
            if len(tokens & best[0]) / len(tokens | best[0]) < 0.5:
                continue
            matched += 1
            used.add(best[1])
            if args.dry_run:
                if p.sku.startswith(("PS-", "RMX-", "BRK-")) and matched % 4 == 0:
                    print(f"  {p.name[:75]}  ←  {best[2]}")
                continue
            url = save_resized(best[1])
            p.image_url = url
            if p.sku in mps:
                mps[p.sku].image = url
        if not args.dry_run:
            db.commit()
    print(f"товаров с фото: {matched}, использовано файлов: {len(used)}")


if __name__ == "__main__":
    main()
