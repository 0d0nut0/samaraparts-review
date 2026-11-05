import html
import json
import unittest
import tempfile
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from app.services.moba_parser import extract_products_from_html, extract_category_tree, category_page_count
from app.services.price_matching import PriceMatcher, PricingPeer, estimate_price, normalize_name
from app.services.pricing import buyer_prices, calculate_prices, reference_prices, round_money


class MobaExtractionTest(unittest.TestCase):
    def test_attribute_json_handles_quoted_brackets_and_nested_arrays(self):
        products = [{'id': 1, 'article': 'A', 'name': 'Дисплей [OLED] "черный"',
                     'price': 1500, 'discountPrice': 1200, 'tags': [{'name': '[NEW]'}]}]
        value = html.escape(json.dumps(products, ensure_ascii=False), quote=True)
        source = f'<div x-data="{{products: {value}}}"></div>'
        self.assertEqual(extract_products_from_html(source), products)

    def test_legacy_script_and_multiple_arrays_deduplicate(self):
        item = {'id': 1, 'link': '/catalog/a/', 'article': 'A', 'name': 'Товар', 'price': 90}
        source = '<script>let nav=[{"id":2,"title":"menu"}];let a=' + json.dumps([item]) + ';</script>'
        self.assertEqual(extract_products_from_html(source + source), [item])

    def test_category_tree_and_pagination_use_actual_links(self):
        tree = [{'name': 'Parts', 'linkUrl': '/catalog/parts/', 'children': []}]
        self.assertEqual(extract_category_tree('popupCatalog(' + json.dumps(tree) + ')'), tree)
        source = '<a href="/catalog/parts/?page=7">7</a><a href="/catalog/other/?page=50">50</a>'
        source += '<a href="https://evil.example/catalog/parts/?page=99">99</a>'
        self.assertEqual(category_page_count(source, '/catalog/parts/'), 7)


class WholeRublePriceTest(unittest.IsolatedAsyncioTestCase):
    async def test_regular_moba_is_wholesale_not_loyalty(self):
        mp = SimpleNamespace(price=Decimal('1000'), discount_price=Decimal('800'))
        result = await calculate_prices(None, mp, purchase_price=Decimal('700'))
        self.assertEqual(result, dict(base=Decimal(1200), card=Decimal(1200), opt=Decimal(1000), super_opt=Decimal(900)))

    def test_super_floor_never_rounds_below_fifteen_percent(self):
        result = reference_prices(1000, purchase_price=Decimal('800.01'))
        self.assertEqual(result['super_opt'], 921)
        self.assertGreaterEqual(result['super_opt'], Decimal('800.01') * Decimal('1.15'))
        self.assertEqual(reference_prices(1000, purchase_price=1000)['super_opt'], 1150)
        self.assertIsNone(reference_prices(1000)['super_opt'])

    def test_tariff_does_not_stack_pickup_discount(self):
        row = SimpleNamespace(our_base=Decimal(1200), our_card=Decimal(1200), our_opt=Decimal(1000), our_super_opt=Decimal(900))
        for pickup in (True, False):
            self.assertEqual(buyer_prices(row, False, authenticated=True, pickup=pickup)['price'], 1200)
            self.assertEqual(buyer_prices(row, True, authenticated=True, pickup=pickup)['price'], 1000)
            self.assertEqual(buyer_prices(row, False, authenticated=True, pickup=pickup, price_type='super_wholesale')['price'], 900)
        self.assertEqual(buyer_prices(row, True, price_type='super_wholesale')['price'], 1200)
        row.our_super_opt = None
        self.assertEqual(buyer_prices(row, True, authenticated=True, price_type='super_wholesale')['price'], 1000)

    def test_invalid_sources_do_not_become_free_products(self):
        for value in (None, 0, -1, 'NaN', 'Infinity', '0.1'):
            self.assertIsNone(reference_prices(value)['base'])
        self.assertEqual(round_money(Decimal('899.5')), 900)


class PriceMatchingTest(unittest.TestCase):
    def test_minor_spelling_and_word_order_match(self):
        m = PriceMatcher([{'article': 'A', 'name': 'Аккумулятор для iPhone 11 (3110 mAh) - Foxconn', 'price': 900}])
        result = m.match({'name': 'Аккумулятор для Apple iPhone 11 (3110 mAh) - (Foxconn)'})
        self.assertEqual(result.product['article'], 'A')

    def test_model_capacity_quality_and_bundle_are_not_discarded(self):
        m = PriceMatcher([{'article': 'A', 'name': 'Аккумулятор iPhone 11 3110mAh OR100', 'price': 900}])
        for name in ('Аккумулятор iPhone 11 Pro 3110mAh OR100',
                     'Аккумулятор iPhone 11 3510mAh OR100',
                     'Аккумулятор iPhone 11 3110mAh Премиум',
                     'Аккумулятор iPhone 11 3110mAh OR100 комплект 2 шт'):
            self.assertIsNone(m.match({'name': name}).product)

    def test_ambiguous_names_are_not_auto_priced(self):
        m = PriceMatcher([{'article': 'A', 'name': 'Дисплей iPhone 11', 'price': 900},
                          {'article': 'B', 'name': 'Дисплей iPhone 11', 'price': 1000}])
        result = m.match({'name': 'Дисплей iPhone 11'})
        self.assertEqual(result.reason, 'ambiguous')
        self.assertIsNone(result.product)

    def test_plus_model_is_not_the_base_model(self):
        m = PriceMatcher([{'article': 'A', 'name': 'Дисплей Samsung S10+', 'price': 900}])
        self.assertIsNone(m.match({'name': 'Дисплей Samsung S10'}).product)
        self.assertEqual(m.match({'name': 'Дисплей Samsung S10 Plus'}).product['article'], 'A')

    def test_median_markup_requires_real_costs_and_enough_peers(self):
        peers = [PricingPeer(Decimal(100), Decimal(p), 'display', 'Premium')
                 for p in (140, 150, 150, 160, 9999)]
        result = estimate_price(Decimal(200), 'display', 'Premium', peers)
        self.assertEqual(result.retail, 300)
        self.assertEqual(result.multiplier, Decimal('1.5'))
        self.assertEqual(result.scope, 'type_quality_cost')
        self.assertIsNone(estimate_price(None, 'display', 'Premium', peers))
        self.assertIsNone(estimate_price(Decimal(200), 'display', 'Premium', peers[:2]))


class PriceImportTest(unittest.TestCase):
    def test_move_stock_creates_samara_preserves_total_and_is_idempotent(self):
        from app.database import Base
        from app.models import Category, City, Product, Stock, InventoryTransaction
        from app.models.inventory_transaction import TransactionType
        from scripts.sync_catalog_prices import build_plan, apply_plan
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        source = [{'source_row': 3, 'sku': '2001', 'barcode': '2001', 'name': 'Дисплей Xiaomi',
                   'moba_name': '', 'print_name': '', 'quality': 'Premium', 'model': 'Xiaomi'}]
        snapshot = {'complete': True, 'fetched_at': '2026-11-03', 'products': []}
        plan = build_plan(source, snapshot, {'2001': 10}, {})
        plan['stock_barcodes'] = ['2001']
        with tempfile.TemporaryDirectory() as folder, Session(engine) as db:
            category, kazan = Category(name='Дисплеи', slug='displei'), City(name='Казань', slug='kazan')
            db.add_all([category, kazan]); db.flush()
            product = Product(sku='2001', name='Дисплей Xiaomi', slug='2001', category_id=category.id)
            db.add(product); db.flush()
            db.add(Stock(product_id=product.id, city_id=kazan.id, quantity=10)); db.commit()
            apply_plan(db, plan, Path(folder) / 'before-1.json', 'samara', 'kazan', 'Самара'); db.commit()
            apply_plan(db, plan, Path(folder) / 'before-2.json', 'samara', 'kazan', 'Самара'); db.commit()
            samara = db.scalar(select(City).where(City.slug == 'samara'))
            self.assertEqual(db.scalar(select(func.sum(Stock.quantity))), 10)
            self.assertEqual(db.scalar(select(Stock.quantity).where(Stock.city_id == kazan.id)), 0)
            self.assertEqual(db.scalar(select(Stock.quantity).where(Stock.city_id == samara.id)), 10)
            movements = list(db.scalars(select(InventoryTransaction)))
            self.assertEqual(len(movements), 1)
            self.assertEqual(movements[0].type, TransactionType.TRANSFER)
            self.assertEqual(movements[0].quantity, 10)
        engine.dispose()

    def test_repeat_sync_preserves_cost_and_other_warehouse_without_duplicate_receipt(self):
        from app.database import Base
        from app.models import Category, City, Product, MobaPrice, Stock, InventoryTransaction
        from scripts.sync_catalog_prices import build_plan, apply_plan
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        source = [{'source_row': 3, 'sku': '2001', 'barcode': '2001', 'name': 'Дисплей Xiaomi',
                   'moba_name': '', 'print_name': '', 'quality': 'Premium', 'model': 'Xiaomi'}]
        snapshot = {'complete': True, 'fetched_at': '2026-11-03', 'products': [
            {'id': 1, 'article': 'LCD-XMI', 'name': 'Дисплей Xiaomi',
             'price': 1000, 'discountPrice': 800, 'link': '/catalog/displei/test/'}]}
        plan = build_plan(source, snapshot, {'2001': 10}, {'2001': Decimal('700.25')})
        plan['stock_barcodes'] = ['2001']
        with tempfile.TemporaryDirectory() as folder, Session(engine) as db:
            category = Category(name='Дисплеи', slug='displei')
            samara, kazan = City(name='Самара', slug='samara'), City(name='Казань', slug='kazan')
            db.add_all([category, samara, kazan]); db.flush()
            product = Product(sku='2001', name='Дисплей Xiaomi', slug='2001', category_id=category.id)
            db.add(product); db.flush()
            db.add_all([Stock(product_id=product.id, city_id=samara.id, quantity=6),
                        Stock(product_id=product.id, city_id=kazan.id, quantity=3)])
            db.commit()
            apply_plan(db, plan, Path(folder) / 'before-1.json', 'samara'); db.commit()
            apply_plan(db, plan, Path(folder) / 'before-2.json', 'samara'); db.commit()
            self.assertEqual(db.scalar(select(func.count(MobaPrice.id))), 1)
            self.assertEqual(db.scalar(select(func.count(InventoryTransaction.id))), 1)
            self.assertEqual(db.scalar(select(InventoryTransaction.quantity)), 4)
            self.assertEqual(product.purchase_price, Decimal('700.25'))
            mp = db.scalar(select(MobaPrice))
            self.assertEqual(mp.our_base, Decimal(1200))
            self.assertEqual(mp.our_card, Decimal(1200))
            self.assertEqual(db.scalar(select(Stock.quantity).where(Stock.city_id == samara.id)), 10)
            self.assertEqual(db.scalar(select(Stock.quantity).where(Stock.city_id == kazan.id)), 3)
        engine.dispose()


if __name__ == '__main__':
    unittest.main()
