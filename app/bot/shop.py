"""
Бот для покупателя: каталог и поиск, карточка товара, корзина, оформление заказа, мои заказы,
профиль (заявка на опт), переписка с менеджером. Всё на тех же данных и правилах, что сайт:
цены — pricing.buyer_prices, корзина — services.cart, заказ — services.order_service, чат — services.chat.
"""
import asyncio
import json
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.bot.common import (
    BTN_CART, BTN_CATALOG, BTN_CHAT, BTN_ORDERS, BTN_PROFILE, BTN_SEARCH, esc, kb, main_menu, money, site, webapp_url,
)
from app.models import City, MobaPrice, Order, OrderItem, Product, Stock, User, WholesaleApplication
from app.models.chat import ChatMessage, ChatThread
from app.models.wholesale_application import ApplicationStatus
from app.routers.catalog import _stock_map
from app.services import accounts, cart, catalog_tree, chat, order_service, search, telegram
from app.services.pricing import buyer_prices
from app.templating import ORDER_STATUS_LABELS

logger = logging.getLogger(__name__)
router = Router(name="shop")
PAGE = 8


class Search(StatesGroup):
    query = State()


class Checkout(StatesGroup):
    phone = State()
    comment = State()


class Wholesale(StatesGroup):
    details = State()


class ChatMode(StatesGroup):
    active = State()


# ───────────────────────── /start, вход и привязка ─────────────────────────


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, state: FSMContext, db: AsyncSession, user: User):
    await state.clear()
    arg = command.args or ""
    if arg.startswith("login_"):
        await _login_confirm(message, arg[6:], user)
        return
    if arg.startswith("link_"):
        await _link_confirm(message, arg[5:], db)
        return
    if arg.startswith("p_"):  # ссылка на товар: t.me/<бот>?start=p_<id>
        await message.answer("Открываю товар…", reply_markup=main_menu(user))
        await _product_card(message, db, user, int(arg[2:]) if arg[2:].isdigit() else 0)
        return
    price = "оптовые" if user.is_wholesale else "розничные"
    await message.answer(
        f"👋 <b>Parts.Site</b> — запчасти для ремонта телефонов, планшетов, ноутбуков и консолей.\n\n"
        f"Напишите модель и деталь, например <i>«iPhone 11 дисплей»</i> или <i>«realme c67 акб»</i>, — найду в каталоге.\n"
        f"Каталог, корзина и заказы — те же, что на сайте; для вас действуют <b>{price} цены</b>.",
        reply_markup=main_menu(user),
    )


# Вход и привязка — только после подтверждения кнопкой. Иначе ссылку, созданную злоумышленником на своём устройстве,
# можно прислать жертве: она нажмёт «Запустить», и злоумышленник войдёт в её аккаунт (или привяжет её Telegram к своему).
WARNING = "⚠️ Если вы сейчас не нажимали эту кнопку на сайте сами — нажмите «Отмена»: кто-то пытается получить доступ."


async def _login_confirm(message: Message, token: str, user: User):
    raw = telegram.peek_token("login", token)
    if not raw or not raw.startswith("?"):
        await message.answer("Ссылка для входа устарела. Нажмите «Войти через Telegram» на сайте ещё раз.",
                             reply_markup=main_menu(user))
        return
    try:
        info = json.loads(raw[1:])
    except ValueError:
        info = {}
    await message.answer(
        f"🔐 <b>Вход на сайт Parts.Site</b> в аккаунт <b>{esc(user.full_name or user.username)}</b>.\n"
        f"Запрос с IP <code>{esc(info.get('ip', '?'))}</code>, браузер: {esc((info.get('ua') or '?')[:80])}\n\n{WARNING}",
        reply_markup=kb([[("✅ Это я — войти", f"lg:ok:{token}"), ("Отмена", f"lg:no:{token}")]]),
    )


@router.callback_query(F.data.regexp(r"^lg:(ok|no):[A-Za-z0-9]+$"))
async def login_decision(cb: CallbackQuery, user: User):
    _, action, token = cb.data.split(":")
    raw = telegram.peek_token("login", token)
    if not raw or not raw.startswith("?"):
        await cb.message.edit_text("Ссылка для входа устарела.")
        await cb.answer()
        return
    if action == "ok":
        telegram.set_token("login", token, user.id)
        await cb.message.edit_text("✅ Готово — вы вошли на сайте. Вернитесь в браузер, страница обновится сама.")
    else:
        telegram.take_token("login", token)
        await cb.message.edit_text("Вход отменён.")
    await cb.answer()


async def _link_confirm(message: Message, token: str, db: AsyncSession):
    target_id = telegram.peek_token("link", token)
    target = (await db.execute(select(User).where(User.id == int(target_id)))).scalar_one_or_none() if target_id else None
    if not target:
        await message.answer("Ссылка для привязки устарела. Нажмите «Привязать Telegram» в личном кабинете ещё раз.")
        return
    await message.answer(
        f"🔗 <b>Привязать этот Telegram к аккаунту «{esc(target.username)}» на сайте?</b>\n"
        "Заказы и переписка из бота перейдут в этот аккаунт, а войти в него можно будет через Telegram.\n\n" + WARNING,
        reply_markup=kb([[("✅ Привязать", f"lk:ok:{token}"), ("Отмена", f"lk:no:{token}")]]),
    )


@router.callback_query(F.data.regexp(r"^lk:(ok|no):[A-Za-z0-9]+$"))
async def link_decision(cb: CallbackQuery, db: AsyncSession, user: User):
    _, action, token = cb.data.split(":")
    if action == "no":
        telegram.take_token("link", token)
        await cb.message.edit_text("Привязка отменена.")
        await cb.answer()
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await _link(cb.message, token, db, user, cb.from_user)
    await cb.answer()


async def _link(message: Message, token: str, db: AsyncSession, user: User, tg_user=None):
    """Привязка Telegram к аккаунту сайта (ссылка из личного кабинета)."""
    target_id = telegram.peek_token("link", token)
    target = (await db.execute(select(User).where(User.id == int(target_id)))).scalar_one_or_none() if target_id else None
    if not target:
        await message.answer("Ссылка для привязки устарела. Нажмите «Привязать Telegram» в личном кабинете ещё раз.")
        return
    try:
        tg_user = tg_user or message.from_user
        await accounts.link_telegram(db, target, tg_user.id, tg_user.full_name)
    except accounts.AccountError as e:
        await message.answer(f"❌ {esc(e)}")
        return
    telegram.take_token("link", token)
    await db.refresh(target)
    await message.answer(f"✅ Telegram привязан к аккаунту <b>{esc(target.username)}</b>. Сюда будут приходить статусы заказов "
                         "и ответы менеджера, а пароль можно восстановить через бота.", reply_markup=main_menu(target))


# ───────────────────────── Каталог ─────────────────────────


@router.message(F.text == BTN_CATALOG)
@router.message(Command("catalog"))
async def catalog_root(message: Message, db: AsyncSession, user: User):
    await message.answer(*await _sections(db))


@router.callback_query(F.data == "sec")
async def catalog_root_cb(cb: CallbackQuery, db: AsyncSession):
    text, markup = await _sections(db)
    await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer()


async def _sections(db: AsyncSession):
    tree = await catalog_tree.get_tree(db)
    rows = [[(f"{n.icon or ''} {n.name} · {n.count}".strip(), f"c:{n.id}:0")] for n in tree.sections]
    url = webapp_url()
    if url:
        rows.append([("🛍 Открыть магазин целиком", "url:" + url)])
    return "📂 <b>Каталог</b>\nВыберите раздел или просто напишите, что ищете.", kb(rows)


@router.callback_query(F.data.startswith("c:"))
async def category(cb: CallbackQuery, db: AsyncSession, user: User):
    _, cid, page = cb.data.split(":")
    tree = await catalog_tree.get_tree(db)
    node = tree.by_id.get(int(cid))
    if not node:
        await cb.answer("Раздел не найден", show_alert=True)
        return
    back = f"c:{node.parent_id}:0" if node.parent_id else "sec"
    if node.children and int(page) == 0:
        rows = [[(f"{c.name} · {c.count}", f"c:{c.id}:0")] for c in node.children if c.count]
        rows.append([("Все товары раздела", f"cl:{node.id}:0")])
        rows.append([("← Назад", back)])
        await cb.message.edit_text(f"📂 <b>{esc(node.name)}</b>", reply_markup=kb(rows))
        await cb.answer()
        return
    await _category_list(cb, db, user, node, 0, back)


@router.callback_query(F.data.startswith("cl:"))
async def category_list(cb: CallbackQuery, db: AsyncSession, user: User):
    _, cid, page = cb.data.split(":")
    tree = await catalog_tree.get_tree(db)
    node = tree.by_id.get(int(cid))
    if not node:
        await cb.answer("Раздел не найден", show_alert=True)
        return
    await _category_list(cb, db, user, node, int(page), f"c:{node.id}:0" if node.children else
                         (f"c:{node.parent_id}:0" if node.parent_id else "sec"))


async def _category_list(cb: CallbackQuery, db: AsyncSession, user: User, node, page: int, back: str):
    tree = await catalog_tree.get_tree(db)
    cond = [Product.category_id.in_(tree.descendant_ids(node))]
    text, markup = await _product_list(db, user, cond, [search.availability().desc(), MobaPrice.id.desc()], page,
                                       f"📂 <b>{esc(node.name)}</b>", f"cl:{node.id}", back)
    await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer()


async def _product_list(db, user, cond, order, page, title, page_prefix, back=None):
    # Товары с карточкой склада и не скрытые — те же, что видит витрина
    cond = [*cond, Product.is_active == 1]
    total = (await db.execute(select(func.count()).select_from(MobaPrice).join(Product, Product.sku == MobaPrice.sku)
                              .where(*cond))).scalar_one()
    rows_db = (
        await db.execute(
            select(MobaPrice, Product.id).join(Product, Product.sku == MobaPrice.sku)
            .where(*cond).order_by(*order).offset(page * PAGE).limit(PAGE)
        )
    ).all()
    stocks = await _stock_map(db, [mp.sku for mp, _ in rows_db], None)
    rows = []
    for mp, pid in rows_db:
        price = buyer_prices(mp, user.is_wholesale, authenticated=True, pickup=True, price_type=user.effective_price_type)["price"]
        st = stocks.get(mp.sku)
        mark = "🟢" if st and st.total > 0 else "⚪️"
        rows.append([(f"{mark} {mp.name[:40]} — {money(price) if price is not None else 'цена по запросу'}", f"p:{pid}")])
    nav = []
    if page > 0:
        nav.append(("◀", f"{page_prefix}:{page - 1}"))
    pages = max(1, (total + PAGE - 1) // PAGE)
    if pages > 1:
        nav.append((f"{page + 1}/{pages}", "noop"))
    if page + 1 < pages:
        nav.append(("▶", f"{page_prefix}:{page + 1}"))
    rows.append(nav)
    if back:
        rows.append([("← Назад", back)])
    head = f"{title}\nНайдено: {total}. 🟢 — есть на складе." if total else f"{title}\nНичего не нашлось. Напишите менеджеру — подберём."
    if not total:
        rows.append([("💬 Спросить менеджера", "chat:me:reply")])
    return head, kb(rows)


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


# ───────────────────────── Поиск ─────────────────────────


@router.message(F.text == BTN_SEARCH)
@router.message(Command("search"))
async def search_prompt(message: Message, state: FSMContext):
    await state.set_state(Search.query)
    await message.answer("Напишите модель и деталь или артикул: <i>«iPhone 13 pro max шлейф»</i>, <i>«honor 10i дисплей в рамке»</i>.")


async def _search(message_or_cb, db, user, state: FSMContext, query: str, page: int, edit: bool = False):
    await state.update_data(q=query)
    cond = search.conditions(query)
    text, markup = await _product_list(db, user, cond, search.order_by(query), page, f"🔍 <b>{esc(query)}</b>", "s")
    if edit:
        await message_or_cb.message.edit_text(text, reply_markup=markup)
    else:
        await message_or_cb.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("s:"))
async def search_page(cb: CallbackQuery, db: AsyncSession, user: User, state: FSMContext):
    q = (await state.get_data()).get("q")
    if not q:
        await cb.answer("Повторите поиск", show_alert=True)
        return
    await _search(cb, db, user, state, q, int(cb.data[2:]), edit=True)
    await cb.answer()


# ───────────────────────── Товар ─────────────────────────


@router.callback_query(F.data.startswith("p:"))
async def product_cb(cb: CallbackQuery, db: AsyncSession, user: User):
    await _product_card(cb.message, db, user, int(cb.data[2:]))
    await cb.answer()


async def _product_card(message: Message, db: AsyncSession, user: User, pid: int):
    row = (await db.execute(select(Product, MobaPrice).join(MobaPrice, MobaPrice.sku == Product.sku)
                            .where(Product.id == pid, Product.is_active == 1))).first()
    if not row:
        await message.answer("Товар не найден или снят с продажи.")
        return
    product, mp = row
    bp = buyer_prices(mp, user.is_wholesale, authenticated=True, pickup=True, price_type=user.effective_price_type)
    stocks = (await db.execute(select(City.name, Stock.quantity).join(Stock, Stock.city_id == City.id)
                               .where(Stock.product_id == pid).order_by(City.id))).all()
    stock_text = "\n".join(f"• {esc(c)}: {'<b>' + str(q) + ' шт.</b>' if q > 0 else 'нет'}" for c, q in stocks) or "• нет на складе"
    if bp["price"] is None:
        price_text = "💰 <b>Цена по запросу</b> — менеджер назовёт цену и зарезервирует товар."
    else:
        price_text = (f"💰 <b>{money(bp['price'])}</b>{' (опт)' if bp['wholesale'] else ''}\n"
                      "Самовывоз · цена по Вашему тарифу")
    text = f"<b>{esc(mp.name)}</b>\n<code>{esc(mp.sku)}</code>\n\n{price_text}\n\n📦 Наличие:\n{stock_text}"
    in_stock = any(q > 0 for _, q in stocks)
    rows = []
    if bp["price"] is not None and in_stock:
        rows.append([("🛒 В корзину", f"add:{pid}")])
    rows.append([("💬 Спросить менеджера", f"ask:{pid}"), ("На сайте", "url:" + site(f"/catalog/{mp.sku}"))])
    if mp.image and mp.image.startswith("http") and len(text) <= 1024:
        try:
            await message.answer_photo(mp.image, caption=text, reply_markup=kb(rows))
            return
        except Exception:  # Telegram не смог скачать картинку — покажем карточку без фото
            logger.info("Фото товара %s не отправилось", mp.sku)
    await message.answer(text, reply_markup=kb(rows))


@router.callback_query(F.data.startswith("add:"))
async def add(cb: CallbackQuery, db: AsyncSession, user: User):
    pid = int(cb.data[4:])
    sku = (await db.execute(select(Product.sku).where(Product.id == pid))).scalar_one_or_none()
    qty = await cart.user_add(db, user.id, sku, 1) if sku else 0
    if not qty:
        await cb.answer("Этот товар сейчас нельзя купить — спросите менеджера", show_alert=True)
        return
    count = await cart.user_count(db, user.id)
    await cb.answer(f"Добавлено, в корзине {qty} шт. этого товара (всего {count})")


@router.callback_query(F.data.startswith("ask:"))
async def ask(cb: CallbackQuery, db: AsyncSession, user: User, state: FSMContext):
    pid = int(cb.data[4:])
    mp = (await db.execute(select(MobaPrice).join(Product, Product.sku == MobaPrice.sku).where(Product.id == pid))).scalar_one_or_none()
    await state.set_state(ChatMode.active)
    await state.update_data(about=f"{mp.name} ({mp.sku})" if mp else "")
    await cb.message.answer(f"Напишите вопрос по товару{(' «' + esc(mp.name) + '»') if mp else ''} — передам менеджеру.",
                            reply_markup=_chat_keyboard())
    await cb.answer()


# ───────────────────────── Корзина и оформление ─────────────────────────


@router.message(F.text == BTN_CART)
@router.message(Command("cart"))
async def cart_view(message: Message, db: AsyncSession, user: User):
    text, markup = await _cart(db, user)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "cart")
async def cart_view_cb(cb: CallbackQuery, db: AsyncSession, user: User):
    text, markup = await _cart(db, user)
    await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


async def _cart(db: AsyncSession, user: User):
    items = await cart.user_cart_items(db, user.id, user.is_wholesale, price_type=user.effective_price_type)
    if not items:
        return "🛒 Корзина пуста. Напишите, что ищете, или откройте каталог.", kb([[("📂 Каталог", "sec")]])
    pids = dict((await db.execute(select(Product.sku, Product.id).where(Product.sku.in_([i["sku"] for i in items])))).all())
    totals = cart.cart_totals(items)
    lines, rows = [], []
    for n, i in enumerate(items, 1):
        lines.append(f"{n}. {esc(i['name'])}\n    {i['qty']} × {money(i['price'])} = <b>{money(i['sum'])}</b>")
        pid = pids[i["sku"]]
        rows.append([(f"{n}.", "noop"), ("➖", f"dec:{pid}"), (f"{i['qty']} шт.", "noop"), ("➕", f"inc:{pid}"), ("🗑", f"del:{pid}")])
    rows.append([("✅ Оформить заказ", "co")])
    rows.append([("Очистить", "clear")])
    opt = " (оптовые цены)" if user.is_wholesale else ""
    text = ("🛒 <b>Корзина</b>" + opt + "\n\n" + "\n".join(lines) +
            f"\n\nИтого: <b>{money(totals['total'])}</b>\nСамовывоз · цена по Вашему тарифу")
    return text, kb(rows)


@router.callback_query(F.data.regexp(r"^(inc|dec|del):\d+$"))
async def cart_change(cb: CallbackQuery, db: AsyncSession, user: User):
    action, pid = cb.data.split(":")
    sku = (await db.execute(select(Product.sku).where(Product.id == int(pid)))).scalar_one_or_none()
    items = {i["sku"]: i["qty"] for i in await cart.user_cart_items(db, user.id, user.is_wholesale, price_type=user.effective_price_type)}
    if sku in items:
        qty = {"inc": items[sku] + 1, "dec": items[sku] - 1, "del": 0}[action]
        await cart.user_set(db, user.id, sku, qty)
    text, markup = await _cart(db, user)
    await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer()


@router.callback_query(F.data == "clear")
async def cart_clear(cb: CallbackQuery, db: AsyncSession, user: User):
    await cart.user_clear(db, user.id)
    await cb.message.edit_text("🛒 Корзина очищена.")
    await cb.answer()


@router.callback_query(F.data == "co")
async def checkout_start(cb: CallbackQuery, db: AsyncSession, user: User, state: FSMContext):
    items = await cart.user_cart_items(db, user.id, user.is_wholesale, price_type=user.effective_price_type)
    if not items:
        await cb.answer("Корзина пуста", show_alert=True)
        return
    # Где можно собрать весь заказ — как на сайте при оформлении
    need = {i["sku"]: i["qty"] for i in items}
    cities = (await db.execute(select(City).where(City.is_active.is_(True)).order_by(City.id))).scalars().all()
    stock = (await db.execute(select(Product.sku, Stock.city_id, Stock.quantity).join(Stock, Stock.product_id == Product.id)
                              .where(Product.sku.in_(list(need))))).all()
    short = {c.id: len(need) for c in cities}
    for sku, city_id, qty in stock:
        if city_id in short and qty >= need[sku]:
            short[city_id] -= 1
    rows = [[(f"{c.name} — {'всё в наличии' if short[c.id] == 0 else 'не хватает ' + str(short[c.id]) + ' поз.'}", f"co:city:{c.id}")]
            for c in cities]
    await state.update_data(co={})
    await cb.message.answer("📍 <b>Шаг 1 из 3.</b> Где заберёте заказ? Товар резервируется на складе этого города.", reply_markup=kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("co:city:"))
async def checkout_city(cb: CallbackQuery, user: User, state: FSMContext):
    await state.update_data(city_id=int(cb.data.split(":")[2]))
    await state.set_state(Checkout.phone)
    buttons = [[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]]
    if user.phone:
        buttons.insert(0, [KeyboardButton(text=user.phone)])
    await cb.message.answer("📞 <b>Шаг 2 из 3.</b> Телефон для связи — позвоним, чтобы подтвердить заказ.",
                            reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True))
    await cb.answer()


@router.message(Checkout.phone)
async def checkout_phone(message: Message, db: AsyncSession, user: User, state: FSMContext):
    phone = message.contact.phone_number if message.contact else (message.text or "").strip()
    if sum(ch.isdigit() for ch in phone) < 5:
        await message.answer("Похоже, это не телефон. Отправьте номер, например +7 900 123-45-67.")
        return
    await state.update_data(phone=phone[:50])
    if not user.phone:
        user.phone = phone[:50]
        await db.commit()
    await state.set_state(None)
    await message.answer("Принято.", reply_markup=main_menu(user))
    await _checkout_confirm(message, db, user, state)


async def _checkout_confirm(message: Message, db: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    items = await cart.user_cart_items(db, user.id, user.is_wholesale, price_type=user.effective_price_type)
    city = await db.get(City, data.get("city_id"))
    totals = cart.cart_totals(items)
    lines = "\n".join(f"• {esc(i['name'])} — {i['qty']} × {money(i['price'])}" for i in items)
    comment = data.get("comment")
    text = (f"🧾 <b>Шаг 3 из 3. Проверьте заказ</b>\n\n{lines}\n\n<b>Итого: {money(totals['total'])}</b>"
            f"{' (опт)' if user.is_wholesale else ''}\nСамовывоз · цена по Вашему тарифу\n\n"
            f"Склад: {esc(city.name if city else '—')}\nТелефон: {esc(data.get('phone'))}"
            + (f"\nКомментарий: {esc(comment)}" if comment else "") +
            f"\n\n<i>Подтверждая заказ, вы соглашаетесь на обработку персональных данных — "
            f"<a href=\"{site('/privacy')}\">политика конфиденциальности</a>.</i>")
    await message.answer(text, reply_markup=kb([
        [("✅ Подтвердить заказ", "co:ok")],
        [("✏️ Комментарий", "co:comment"), ("Отмена", "co:cancel")],
    ]))


@router.callback_query(F.data == "co:comment")
async def checkout_comment(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Checkout.comment)
    await cb.message.answer("Напишите комментарий: модель устройства, удобное время звонка, адрес доставки…")
    await cb.answer()


@router.message(Checkout.comment)
async def checkout_comment_text(message: Message, db: AsyncSession, user: User, state: FSMContext):
    await state.update_data(comment=(message.text or "").strip()[:1000])
    await state.set_state(None)
    await _checkout_confirm(message, db, user, state)


@router.callback_query(F.data == "co:cancel")
async def checkout_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Оформление отменено, корзина сохранена.")
    await cb.answer()


_checkout_locks: dict[int, asyncio.Lock] = {}


@router.callback_query(F.data == "co:ok")
async def checkout_ok(cb: CallbackQuery, db: AsyncSession, user: User, state: FSMContext):
    # Обновления обрабатываются параллельно: двойное нажатие не должно создать два заказа
    lock = _checkout_locks.setdefault(user.id, asyncio.Lock())
    if lock.locked():
        await cb.answer("Заказ уже оформляется…")
        return
    async with lock:
        await _checkout_ok(cb, db, user, state)


async def _checkout_ok(cb: CallbackQuery, db: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    if not data.get("city_id") or not data.get("phone"):
        await cb.answer("Начните оформление заново из корзины", show_alert=True)
        return
    items = await cart.user_cart_items(db, user.id, user.is_wholesale, price_type=user.effective_price_type)
    if not items:
        await cb.answer("Корзина пуста", show_alert=True)
        return
    try:
        order = await order_service.create_order(
            db=db, user_id=user.id, city_id=data["city_id"],
            cart_items=[{"sku": i["sku"], "qty": i["qty"], "price": i["price"]} for i in items],
            customer_name=(user.full_name or cb.from_user.full_name or user.username)[:200],
            customer_phone=data["phone"], wholesale=user.is_wholesale,
            comment=data.get("comment") or None, source="bot",
        )
        await cart.user_clear(db, user.id, commit=False)
        await db.commit()
    except order_service.OrderError as e:
        await cb.message.answer(f"❌ {esc(e)}")
        await cb.answer()
        return
    await state.clear()
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer("Заказ оформлен")
    await telegram.notify_new_order(order.id)  # покупателю — подтверждение, администраторам — уведомление


# ───────────────────────── Мои заказы ─────────────────────────


@router.message(F.text == BTN_ORDERS)
@router.message(Command("orders"))
async def my_orders(message: Message, db: AsyncSession, user: User):
    orders = (await db.execute(select(Order).options(selectinload(Order.items).selectinload(OrderItem.product))
                               .where(Order.user_id == user.id).order_by(Order.id.desc()).limit(10))).scalars().all()
    if not orders:
        await message.answer("Заказов пока нет.")
        return
    parts = []
    for o in orders:
        items = ", ".join(f"{esc(i.product.name[:30]) if i.product else '—'} × {i.quantity}" for i in o.items[:3])
        more = f" и ещё {len(o.items) - 3}" if len(o.items) > 3 else ""
        parts.append(f"<b>№ {o.id}</b> от {o.created_at:%d.%m} · {ORDER_STATUS_LABELS[o.status.name]} · <b>{money(o.total_amount)}</b>\n{items}{more}")
    await message.answer("📦 <b>Мои заказы</b>\n\n" + "\n\n".join(parts),
                         reply_markup=kb([[("Все заказы на сайте", "url:" + site("/auth/account"))]]))


# ───────────────────────── Профиль и опт ─────────────────────────


@router.message(F.text == BTN_PROFILE)
@router.message(Command("profile"))
async def profile(message: Message, db: AsyncSession, user: User):
    pending = (await db.execute(select(WholesaleApplication.id).where(
        WholesaleApplication.user_id == user.id, WholesaleApplication.status == ApplicationStatus.PENDING))).first()
    status = ("оптовый покупатель — оптовые цены" if user.is_wholesale else "администратор" if user.is_superuser
              else "покупатель — розничные цены")
    text = (f"👤 <b>{esc(user.full_name or user.username)}</b>\nСтатус: {status}\n"
            f"Телефон: {esc(user.phone or 'не указан')}\nЛогин на сайте: <code>{esc(user.username)}</code>")
    rows = []
    if not user.is_wholesale and not user.is_superuser:
        rows.append([("⏳ Заявка на опт на рассмотрении", "noop")] if pending else [("💼 Запросить оптовые цены", "ws:req")])
    rows.append([("Войти на сайте", "url:" + site("/auth/telegram")), ("Конфиденциальность", "url:" + site("/privacy"))])
    if not accounts.has_password(user):
        text += "\n\nНа сайт можно входить через Telegram, а пароль — задать в личном кабинете."
    await message.answer(text, reply_markup=kb(rows))


@router.callback_query(F.data == "ws:req")
async def wholesale_request(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Wholesale.details)
    await cb.message.answer("Расскажите о себе: название сервиса или магазина, ИНН, город, примерный объём закупок в месяц.")
    await cb.answer()


@router.message(Wholesale.details)
async def wholesale_details(message: Message, db: AsyncSession, user: User, state: FSMContext):
    details = (message.text or "").strip()
    if len(details) < 3:
        await message.answer("Напишите чуть подробнее.")
        return
    await state.clear()
    application = WholesaleApplication(user_id=user.id, company_details=details[:2000])
    db.add(application)
    await db.commit()
    await message.answer("✅ Заявка отправлена. Обычно отвечаем в течение рабочего дня — пришлём ответ сюда.")
    await telegram.notify_wholesale(application.id)


# ───────────────────────── Переписка с менеджером ─────────────────────────


def _chat_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="✖️ Закончить переписку")]], resize_keyboard=True)


@router.message(F.text == BTN_CHAT)
@router.callback_query(F.data == "chat:me:reply")
async def chat_start(event, db: AsyncSession, user: User, state: FSMContext):
    message = event.message if isinstance(event, CallbackQuery) else event
    await state.set_state(ChatMode.active)
    thread = await _own_thread(db, user, create=False)
    history = ""
    if thread:
        last = (await db.execute(select(ChatMessage).where(ChatMessage.thread_id == thread.id)
                                 .order_by(ChatMessage.id.desc()).limit(4))).scalars().all()
        if last:
            history = "\n\nПоследние сообщения:\n" + "\n".join(
                f"{'Вы' if m.sender == 'client' else 'Менеджер' if m.sender == 'staff' else '•'}: {esc(m.text[:200])}"
                for m in reversed(last))
    await message.answer("💬 Напишите сообщение — менеджер ответит здесь. Переписка та же, что в чате на сайте." + history,
                         reply_markup=_chat_keyboard())
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.message(ChatMode.active, F.text == "✖️ Закончить переписку")
async def chat_stop(message: Message, user: User, state: FSMContext):
    await state.clear()
    await message.answer("Хорошо. Если появятся вопросы — нажмите «💬 Менеджер».", reply_markup=main_menu(user))


@router.message(ChatMode.active, F.text)
async def chat_message(message: Message, db: AsyncSession, user: User, state: FSMContext):
    if chat.rate_limited(("tg", user.id)):
        await message.answer("Слишком часто — подождите несколько секунд.")
        return
    thread = await _own_thread(db, user, create=True)
    about = (await state.get_data()).get("about")
    text = (f"Вопрос по товару: {about}\n" if about else "") + message.text
    await state.update_data(about=None)
    first_unread = not thread.unread_staff
    thread.last_page = "Telegram"
    try:
        await chat.add_message(db, thread, "client", text)
    except chat.ChatError as e:
        await message.answer(str(e))
        return
    thread.unread_client = 0
    await db.commit()
    await message.answer("✓ Передал менеджеру.")
    if first_unread:
        await telegram.notify_chat_message(thread.id, text)


@router.message(ChatMode.active)
async def chat_not_text(message: Message):
    await message.answer("Пока передаю менеджеру только текст. Опишите вопрос словами или пришлите артикул.")


async def _own_thread(db: AsyncSession, user: User, create: bool) -> ChatThread | None:
    thread = (await db.execute(select(ChatThread).where(ChatThread.user_id == user.id)
                               .order_by(ChatThread.last_message_at.desc()).limit(1))).scalar_one_or_none()
    if thread or not create:
        return thread
    thread = ChatThread(user_id=user.id, name=user.full_name, phone=user.phone)
    db.add(thread)
    await db.flush()
    return thread


# ───────────────────────── Текст без команды — поиск ─────────────────────────


@router.message(Search.query, F.text)
@router.message(F.text & ~F.text.startswith("/"))
async def free_text(message: Message, db: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    query = message.text.strip()[:100]
    if len(query) < 2:
        await message.answer("Напишите модель и деталь, например «iPhone 11 дисплей».")
        return
    await _search(message, db, user, state, query, 0)
