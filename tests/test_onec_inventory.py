import unittest
from app.services.onec_inventory import stock_quantities, apply_stock_snapshot
from app.services.onec_odata import ZERO
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.database import Base
from app.models import Category, City, Product, Stock, Order, OrderItem
from app.models.order import OrderStatus
from tests.test_onec_price_cycle import AsyncAdapter


class StockSnapshotTest(unittest.IsolatedAsyncioTestCase):
    def source(self, qty=8, reserved=2):
        return {'complete':True,'warehouses':[{'Ref_Key':'wh'}], 'balances':[
            {'Номенклатура_Key':'goods','Склад_Key':'wh','Характеристика_Key':ZERO,
             'ВНаличииBalance':qty,'КОтгрузкеBalance':reserved}]}

    async def test_absolute_import_zeroes_disappeared_items_and_preserves_unexported_hold(self):
        engine=create_engine('sqlite:///:memory:');Base.metadata.create_all(engine)
        with Session(engine) as db:
            category=Category(name='Parts',slug='parts');city=City(name='Самара',slug='samara',onec_warehouse_id='wh')
            db.add_all([category,city]);db.flush()
            product=Product(sku='p',name='Part',slug='p',category_id=category.id,onec_id='goods')
            other=Product(sku='q',name='Other',slug='q',category_id=category.id,onec_id='other')
            db.add_all([product,other]);db.flush()
            db.add_all([Stock(product_id=product.id,city_id=city.id,quantity=999),Stock(product_id=other.id,city_id=city.id,quantity=999)])
            order=Order(city_id=city.id,total_amount=100,customer_name='Buyer',customer_phone='test',status=OrderStatus.PENDING)
            order.items.append(OrderItem(product_id=product.id,quantity=1,price_at_time=100));db.add(order);db.flush()
            adapter=AsyncAdapter(db)
            result=await apply_stock_snapshot(adapter,self.source());db.flush()
            self.assertEqual(result['stock_units'],5)
            self.assertEqual(db.scalar(select(Stock.quantity).where(Stock.product_id==other.id)),0)
            result=await apply_stock_snapshot(adapter,self.source());db.flush()
            self.assertEqual(result['stock_changed_rows'],0)
            order.status=OrderStatus.CANCELLED;db.flush()
            self.assertEqual((await apply_stock_snapshot(adapter,self.source()))['stock_units'],6)
        engine.dispose()

    def test_bad_source_never_silently_changes_quantities(self):
        self.assertEqual(stock_quantities(self.source(),{'wh':2},{'goods':4}),{(4,2):6})
        for source,mapping in [(self.source(),{}),(self.source(1.5),{'wh':2}),({'complete':False},{'wh':2})]:
            with self.assertRaises(ValueError):stock_quantities(source,mapping,{'goods':4})
