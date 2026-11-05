"""Run: python -m unittest discover -s tests -v.

Integration test opts in only with PARTS_TEST_DATABASE_URL pointing at an expendable
PostgreSQL database whose name ends in _test, and PARTS_TEST_REDIS_URL (empty test DB).
Apply migrations to that database before running. No external mail or Telegram is sent.
"""
import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

TEST_DB = os.environ.get("PARTS_TEST_DATABASE_URL")
if TEST_DB:
    from sqlalchemy.engine import make_url
    if not make_url(TEST_DB).database.endswith("_test"):
        raise RuntimeError("Integration database name must end in _test")
    os.environ["DATABASE_URL"] = TEST_DB
    os.environ["REDIS_URL"] = os.environ["PARTS_TEST_REDIS_URL"]

from app.services.pricing import buyer_prices, round_money
from app.services.passwords import password_error
from app.services.email_verification import normalize_email


def product(base="1100", retail="1100", opt="800"):
    return SimpleNamespace(our_base=Decimal(base) if base else None,
                           our_card=Decimal(retail) if retail else None,
                           our_opt=Decimal(opt) if opt else None)


class PricesTest(unittest.TestCase):
    def test_guest_sees_full_price(self):
        self.assertEqual(buyer_prices(product(), False)["price"], Decimal("1100"))

    def test_guest_cannot_claim_pickup_discount(self):
        bp = buyer_prices(product(), False, pickup=True)
        self.assertEqual(bp["price"], Decimal("1100"))
        self.assertFalse(bp["discount"])

    def test_logged_pickup(self):
        self.assertEqual(buyer_prices(product(), False, authenticated=True, pickup=True)["price"], Decimal("1100"))

    def test_logged_delivery(self):
        self.assertEqual(buyer_prices(product(), False, authenticated=True, pickup=False)["price"], Decimal("1100"))

    def test_wholesale_requires_authenticated_account(self):
        self.assertEqual(buyer_prices(product(), True)["price"], Decimal("1100"))
        self.assertEqual(buyer_prices(product(), True, authenticated=True)["price"], Decimal("800"))

    def test_wholesale_pickup(self):
        self.assertEqual(buyer_prices(product(), True, authenticated=True, pickup=True)["price"], Decimal("800"))

    def test_absent_retail_falls_back_to_base(self):
        self.assertEqual(buyer_prices(product(base="1000", retail=None), False)["price"], Decimal("1000"))

    def test_unpriced_product_not_zero(self):
        self.assertIsNone(buyer_prices(product(base=None), False)["price"])

    def test_rounding_per_unit(self):
        self.assertEqual(buyer_prices(product(base="11.15", retail="11.15"), False, authenticated=True, pickup=True)["price"], Decimal("11"))


class PasswordsTest(unittest.TestCase):
    def test_valid(self):
        self.assertIsNone(password_error("GreenPart9!", "GreenPart9!"))

    def test_no_confirmation(self):
        self.assertEqual(password_error("GreenPart9!", ""), "Пароли не совпадают")

    def test_wrong_confirmation(self):
        self.assertEqual(password_error("GreenPart9!", "GreenPart8!"), "Пароли не совпадают")

    def test_weak_passwords(self):
        for p in ("abcdefgh", "Password", "Password1", "password1!", "PASSWORD1!", "Pass1!", " ", "Aa1!" * 33):
            with self.subTest(password_type=len(p)):
                self.assertIsNotNone(password_error(p, p))

    def test_unicode_letters(self):
        self.assertIsNone(password_error("ЗелёныйПартс9!", "ЗелёныйПартс9!"))


class EmailTest(unittest.TestCase):
    def test_normalized(self):
        self.assertEqual(normalize_email(" Owner+Shop@Example.COM "), "owner+shop@example.com")

    def test_invalid(self):
        for address in ("bad", "a@", "a@@b.ru", "a@localhost", "a\nb@ex.ru", ".a@ex.ru", "a..b@ex.ru", "a@-ex.ru", "a@ex..ru", "a@ex.ru\r\nBcc:x@y.ru"):
            with self.subTest(address=address):
                self.assertIsNone(normalize_email(address))


class MailTransportTest(unittest.TestCase):
    def test_starttls(self):
        from app.config import settings
        from app.services import email_verification as mail
        smtp = MagicMock()
        with patch.object(settings, "SMTP_SSL", False), patch.object(settings, "SMTP_STARTTLS", True), \
             patch.object(settings, "SMTP_USERNAME", "shop"), patch.object(settings, "SMTP_FROM", "shop@example.com"), \
             patch.object(mail.smtplib, "SMTP", return_value=smtp):
            mail._send("buyer@example.com", "Buyer", "https://shop.example/auth/verify-email?token=test")
        connection = smtp.__enter__.return_value
        connection.starttls.assert_called_once()
        connection.login.assert_called_once()
        message = connection.send_message.call_args.args[0]
        self.assertEqual(message["To"], "buyer@example.com")
        self.assertIn("token=test", message.get_body(preferencelist=("plain",)).get_content())
        self.assertIn("ПОДТВЕРДИТЬ", message.get_body(preferencelist=("html",)).get_content())

    def test_implicit_tls(self):
        from app.config import settings
        from app.services import email_verification as mail
        smtp = MagicMock()
        with patch.object(settings, "SMTP_SSL", True), patch.object(settings, "SMTP_FROM", "shop@example.com"), \
             patch.object(mail.smtplib, "SMTP_SSL", return_value=smtp):
            mail._send("buyer@example.com", "Buyer", "https://shop.example/verify")
        smtp.__enter__.return_value.starttls.assert_not_called()
        smtp.__enter__.return_value.send_message.assert_called_once()


@unittest.skipUnless(TEST_DB, "Set isolated PARTS_TEST_DATABASE_URL and PARTS_TEST_REDIS_URL to run integration")
class JourneyTest(unittest.IsolatedAsyncioTestCase):
    async def test_registration_verification_cart_checkout_and_smtp_failure(self):
        import hashlib
        from datetime import datetime, timedelta, timezone
        import httpx
        from sqlalchemy import select, delete
        from app.config import settings
        from app.database import AsyncSessionLocal, engine, Base
        from app.main import app
        from app.models import User, City, Category, Product, MobaPrice, Stock, Order, OrderItem
        from app.routers.auth import get_password_hash
        from app.services import cart, email_verification, telegram, catalog_tree, accounts
        self.assertNotEqual(cart.redis_client.connection_pool.connection_kwargs.get("db", 0), 0,
                            "Integration Redis must not use the application default DB")
        self.assertEqual(cart.redis_client.dbsize(), 0, "Use an empty Redis DB reserved for these tests")
        deliveries = []

        def capture(recipient, username, url):
            deliveries.append((recipient, url))

        async def request(client, method, path, data=None):
            response = await client.request(method, path, data=data)
            self.assertLess(response.status_code, 500, response.text[:1000])
            return response

        try:
            async with AsyncSessionLocal() as db:
                # Explicit opt-in guard above ensures only the expendable integration database is cleared.
                for table in reversed(Base.metadata.sorted_tables):
                    await db.execute(delete(table))
                await db.commit()
                city = City(name="Самара", slug="samara")
                category = Category(name="Дисплеи", slug="displays")
                db.add_all([city, category])
                await db.flush()
                p = Product(sku="TEST-PART", name="Дисплей iPhone 11", slug="test-part", category_id=category.id, is_active=1)
                db.add(p)
                await db.flush()
                db.add_all([MobaPrice(sku=p.sku, name=p.name, price=1000, our_base=1200, our_card=1200, our_opt=1000),
                            Stock(product_id=p.id, city_id=city.id, quantity=30)])
                legacy = User(username="legacy", password_hash=get_password_hash("oldpassword"))
                db.add(legacy)
                await db.commit()
                city_id = city.id
            transport = httpx.ASGITransport(app=app)
            with patch.object(settings, "MAINTENANCE_FILE", "var/isolated-test-maintenance.flag"), \
                 patch.object(settings, "REGISTRATION_EMAIL_ENABLED", True), \
                 patch.object(settings, "SMTP_HOST", "local.test"), patch.object(settings, "SMTP_FROM", "shop@example.com"), \
                 patch.object(email_verification, "_send", capture), patch.object(telegram, "fire", side_effect=lambda coro: coro.close()):
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    response = await request(client, "GET", "/catalog/TEST-PART")
                    self.assertIn('1200.00', response.text)
                    self.assertNotIn("Картой при получении", response.text)
                    await request(client, "POST", "/cart/add", {"sku": "TEST-PART", "qty": "2"})
                    data = {"username": "green_owner", "email": "Owner@Example.com", "password": "GreenPart9!", "password_confirm": "GreenPart9!", "consent": "1", "next": "/checkout"}
                    for bad in ({"password_confirm": "wrong"}, {"password": "weak", "password_confirm": "weak"}, {"email": "bad"}):
                        response = await request(client, "POST", "/auth/register", {**data, **bad})
                        self.assertEqual(response.status_code, 400)
                    response = await request(client, "POST", "/auth/register", data)
                    self.assertIn("Проверьте почту", response.text)
                    self.assertEqual(len(deliveries), 1)
                    self.assertEqual(deliveries[0][0], "owner@example.com")
                    async with AsyncSessionLocal() as db:
                        u = (await db.execute(select(User).where(User.username == "green_owner"))).scalar_one()
                        self.assertFalse(u.email_verified)
                        token = deliveries[0][1].split("token=")[1].split("&")[0]
                        self.assertEqual(u.email_token_hash, hashlib.sha256(token.encode()).hexdigest())
                    response = await request(client, "POST", "/auth/login", {"username": "green_owner", "password": "GreenPart9!"})
                    self.assertEqual(response.status_code, 403)
                    response = await request(client, "GET", "/checkout")
                    self.assertEqual(response.status_code, 303)
                    # GET preview must not consume the link, including repeated mail scanner visits.
                    for _ in range(2):
                        response = await request(client, "GET", f"/auth/verify-email?token={token}")
                        self.assertIn("Подтвердить e-mail", response.text)
                    response = await request(client, "POST", "/auth/verify-email", {"token": token, "next": "//evil.example"})
                    self.assertEqual(response.status_code, 303)
                    self.assertNotIn("evil.example", response.headers["location"])
                    response = await request(client, "POST", "/auth/verify-email", {"token": token})
                    self.assertEqual(response.status_code, 400)
                    response = await request(client, "POST", "/auth/login", {"username": "green_owner", "password": "GreenPart9!"})
                    self.assertEqual(response.status_code, 303)
                    response = await request(client, "GET", "/cart")
                    self.assertNotIn("Включена скидка 10%", response.text)
                    self.assertIn("тарифу", response.text)
                    response = await request(client, "GET", "/checkout")
                    self.assertIn("delivery_method", response.text)
                    response = await request(client, "GET", "/checkout/summary?delivery_method=delivery")
                    self.assertNotIn("Включена скидка", response.text)
                    self.assertIn('1\u202f200', response.text)
                    payload = {"customer_name": "Покупатель", "customer_phone": "89970000000", "city_id": str(city_id), "consent": "1"}
                    response = await request(client, "POST", "/checkout/process", {**payload, "delivery_method": "delivery", "address": ""})
                    self.assertIn("error=", response.headers["location"])
                    response = await request(client, "POST", "/checkout/process", {**payload, "delivery_method": "pickup", "price": "1", "total": "1", "address": "ignored"})
                    self.assertIn("/checkout/success", response.headers["location"])
                    async with AsyncSessionLocal() as db:
                        order = (await db.execute(select(Order).order_by(Order.id.desc()))).scalars().first()
                        self.assertEqual(order.total_amount, Decimal("2400"))
                        self.assertIsNone(order.address)
                        item = (await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalar_one()
                        self.assertEqual(item.price_at_time, Decimal("1200"))
                    await request(client, "POST", "/cart/add", {"sku": "TEST-PART", "qty": "1"})
                    response = await request(client, "POST", "/checkout/process", {**payload, "delivery_method": "delivery", "address": "Самара, Тестовая 1"})
                    self.assertIn("/checkout/success", response.headers["location"])
                    async with AsyncSessionLocal() as db:
                        order = (await db.execute(select(Order).order_by(Order.id.desc()))).scalars().first()
                        self.assertEqual(order.total_amount, Decimal("1200"))
                        self.assertTrue(order.address)
                    # Same password policy enforced when changing it, session stays valid on success.
                    response = await request(client, "POST", "/auth/password", {"old_password": "GreenPart9!", "new_password": "OtherPart9!", "password_confirm": "wrong"})
                    self.assertIn("error=", response.headers["location"])
                    response = await request(client, "POST", "/auth/password", {"old_password": "GreenPart9!", "new_password": "OtherPart9!", "password_confirm": "OtherPart9!"})
                    self.assertIn("msg=", response.headers["location"])
                    self.assertEqual((await request(client, "GET", "/auth/account")).status_code, 200)
                    await request(client, "GET", "/auth/logout")
                    response = await request(client, "POST", "/auth/login", {"username": "legacy", "password": "oldpassword"})
                    self.assertEqual(response.status_code, 303)
                    await request(client, "GET", "/auth/logout")
                    # Reset uses the same policy and does not consume a token on validation errors.
                    async with AsyncSessionLocal() as db:
                        owner = (await db.execute(select(User).where(User.username == "green_owner"))).scalar_one()
                        reset_token = accounts.new_password_reset(owner)
                    response = await request(client, "POST", "/auth/reset", {"token": reset_token, "password": "weak", "password_confirm": "weak"})
                    self.assertIn("8 до 128", response.text)
                    self.assertTrue(telegram.peek_token("reset", reset_token))
                    response = await request(client, "POST", "/auth/reset", {"token": reset_token, "password": "ResetPart9!", "password_confirm": "ResetPart9!"})
                    self.assertEqual(response.status_code, 303)
                    self.assertFalse(telegram.peek_token("reset", reset_token))
                    await request(client, "GET", "/auth/logout")
                    # SMTP failure leaves a pending account, then resend rotates the old token.
                    with patch.object(email_verification, "_send", side_effect=OSError("test SMTP failure")):
                        response = await request(client, "POST", "/auth/register", {**data, "username": "pending", "email": "pending@example.com"})
                        self.assertIn("Не удалось отправить", response.text)
                    async with AsyncSessionLocal() as db:
                        pending = (await db.execute(select(User).where(User.username == "pending"))).scalar_one()
                        self.assertFalse(pending.email_verified)
                        old_hash = pending.email_token_hash
                    response = await request(client, "POST", "/auth/resend-verification", {"email": "pending@example.com"})
                    self.assertIn("Проверьте почту", response.text)
                    async with AsyncSessionLocal() as db:
                        pending = (await db.execute(select(User).where(User.username == "pending"))).scalar_one()
                        self.assertNotEqual(pending.email_token_hash, old_hash)
                        pending.email_token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                        await db.commit()
                    expired_token = deliveries[-1][1].split("token=")[1].split("&")[0]
                    response = await request(client, "POST", "/auth/verify-email", {"token": expired_token})
                    self.assertEqual(response.status_code, 400)
                    # Mail form is rate limited; no account existence disclosure.
                    for _ in range(3):
                        response = await request(client, "POST", "/auth/resend-verification", {"email": "unknown@example.com"})
                    self.assertEqual(response.status_code, 429)
                    with patch.object(settings, "SMTP_HOST", ""):
                        response = await client.post("/auth/register", data={**data, "username": "nomail", "email": "nomail@example.com"})
                        self.assertEqual(response.status_code, 503)
                    for path in ("/catalog", "/auth/register", "/auth/login", "/auth/reset", "/contacts", "/delivery", "/payment", "/warranty", "/privacy", "/about"):
                        self.assertEqual((await request(client, "GET", path)).status_code, 200)
                    self.assertIn("Вами", (await request(client, "GET", "/delivery")).text)
                    self.assertIn("Скоро открытие", (await request(client, "GET", "/contacts")).text)
                    # Launch mode needs no sender, collects no unverified email,
                    # preserves guest carts and cannot grant an elevated tariff.
                    with patch.object(settings, "REGISTRATION_EMAIL_ENABLED", False), patch.object(settings, "SMTP_HOST", ""):
                        await request(client, "GET", "/auth/logout")
                        page = await request(client, "GET", "/auth/register")
                        self.assertNotIn('name="email"', page.text)
                        await request(client, "POST", "/cart/add", {"sku": "TEST-PART", "qty": "1"})
                        before_mail = len(deliveries)
                        for username in ['launch_buyer', 'launch_second']:
                            response = await request(client, "POST", "/auth/register", {
                                "username": username, "password": "GreenPart9!", "password_confirm": "GreenPart9!",
                                "consent": "1", "email": "claimed@example.com", "role": "admin", "price_type": "super_wholesale"})
                            self.assertEqual(response.status_code, 303)
                            self.assertEqual((await request(client, "GET", "/auth/account")).status_code, 200)
                            async with AsyncSessionLocal() as db:
                                launch = (await db.execute(select(User).where(User.username == username))).scalar_one()
                                self.assertIsNone(launch.email)
                                self.assertFalse(launch.email_verified)
                                self.assertFalse(launch.is_superuser)
                                self.assertEqual(launch.effective_price_type, 'retail')
                            await request(client, "GET", "/auth/logout")
                        self.assertEqual(len(deliveries), before_mail)
                        response = await request(client, "POST", "/auth/login", {"username": "launch_buyer", "password": "GreenPart9!"})
                        self.assertEqual(response.status_code, 303)
                        self.assertIn('TEST-PART', (await request(client, "GET", "/cart")).text)
                        # Grant admin only in the isolated test DB to test real print routes.
                        async with AsyncSessionLocal() as db:
                            launch = (await db.execute(select(User).where(User.username == 'launch_buyer'))).scalar_one()
                            launch.is_admin = True
                            await db.commit()
                        for suffix, title in [('invoice', 'Счёт на оплату'), ('delivery-note', 'Накладная')]:
                            response = await request(client, 'GET', f'/admin/orders/{order.id}/{suffix}')
                            self.assertEqual(response.status_code, 200)
                            self.assertIn(title, response.text)
                            self.assertIn('Герасимов', response.text)
                            self.assertNotIn('ОБРАЗЕЦ', response.text)
                        # Support recovery requires admin + explicit identity check;
                        # tokens are one-use and revoked by a password change.
                        async with AsyncSessionLocal() as db:
                            second = (await db.execute(select(User).where(User.username == 'launch_second'))).scalar_one()
                            second_id = second.id
                        async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as buyer:
                            await request(buyer, 'POST', '/auth/login', {'username': 'launch_second', 'password': 'GreenPart9!'})
                            denied = await buyer.post(f'/admin/users/{second_id}/password-reset', data={'identity_checked': '1'})
                            self.assertIn(denied.status_code, (303, 403))
                            denied = await request(client, 'POST', f'/admin/users/{second_id}/password-reset')
                            self.assertEqual(denied.status_code, 303)
                            reset_tokens = []
                            import re
                            for _ in range(2):
                                issued = await request(client, 'POST', f'/admin/users/{second_id}/password-reset', {'identity_checked': '1'})
                                self.assertEqual(issued.status_code, 200)
                                reset_tokens.append(re.search(r'/auth/reset\?token=([A-Za-z0-9]+)', issued.text).group(1))
                            for _ in range(2):
                                self.assertIn('name="password"', (await request(buyer, 'GET', f'/auth/reset?token={reset_tokens[0]}')).text)
                            changed = await request(buyer, 'POST', '/auth/reset', {'token': reset_tokens[0], 'password': 'NewGreenPart9!', 'password_confirm': 'NewGreenPart9!'})
                            self.assertEqual(changed.status_code, 303)
                            for reset_token in reset_tokens:
                                rejected = await request(buyer, 'POST', '/auth/reset', {'token': reset_token, 'password': 'WrongGreenPart9!', 'password_confirm': 'WrongGreenPart9!'})
                                self.assertNotEqual(rejected.status_code, 303)
                            self.assertEqual((await request(buyer, 'POST', '/auth/login', {'username': 'launch_second', 'password': 'NewGreenPart9!'})).status_code, 303)
                        async with AsyncSessionLocal() as db:
                            owner = (await db.execute(select(User).where(User.username == 'launch_buyer'))).scalar_one()
                            owner_token = accounts.new_password_reset(owner)
                        async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as recovered:
                            changed = await request(recovered, 'POST', '/auth/reset', {'token': owner_token, 'password': 'OwnerGreenPart9!', 'password_confirm': 'OwnerGreenPart9!'})
                            self.assertEqual(changed.headers['location'], '/admin/dashboard')
                            self.assertEqual((await request(recovered, 'GET', '/admin/dashboard')).status_code, 200)
                            self.assertEqual((await request(client, 'GET', '/admin/dashboard')).status_code, 303)
        finally:
            # The guard + initially empty DB ensure this only removes this run's Redis keys.
            for key in cart.redis_client.scan_iter():
                cart.redis_client.delete(key)
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
