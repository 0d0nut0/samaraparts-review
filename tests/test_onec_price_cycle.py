import unittest
from decimal import Decimal
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from app.database import Base
from app.models import Category, City, Product, MobaPrice, Stock
from app.services import onec_exchange
from app.services.catalog_sync import sync_snapshot
from app.services.pricing import reference_prices, assign_prices
from app.services.invoice import amount_words, invoice_data
from types import SimpleNamespace
from datetime import datetime

class AsyncAdapter:
    def __init__(self, db): self.db=db
    async def execute(self,*a,**kw):return self.db.execute(*a,**kw)
    async def flush(self):self.db.flush()
    def add(self,obj):self.db.add(obj)

class PriceCycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine=create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.db=Session(self.engine)
        cat=Category(name='Дисплеи',slug='display');city=City(name='Самара',slug='samara')
        self.db.add_all([cat,city]);self.db.flush()
        self.product=Product(sku='2000000012345',name='Дисплей Xiaomi',slug='test',category_id=cat.id)
        self.db.add(self.product);self.db.flush()
        self.mp=MobaPrice(sku=self.product.sku,name=self.product.name,price=1000,url='https://moba.ru/catalog/test/',link='https://moba.ru/catalog/test/')
        self.db.add_all([self.mp,Stock(product_id=self.product.id,city_id=city.id,quantity=8)])
        self.db.flush();self.adapter=AsyncAdapter(self.db)
    def tearDown(self):self.db.close();self.engine.dispose()
    async def test_barcode_binds_existing_product_and_repeated_import_is_idempotent(self):
        xml=b'<Root><Tovar/></Root>'
        xml='<КоммерческаяИнформация><Каталог><Товары><Товар><Ид>guid-1</Ид><Наименование>Дисплей Xiaomi</Наименование><Штрихкод>2000000012345</Штрихкод></Товар></Товары></Каталог></КоммерческаяИнформация>'.encode()
        await onec_exchange.import_catalog(self.adapter,xml)
        await onec_exchange.import_catalog(self.adapter,xml)
        self.assertEqual(self.db.scalar(select(func.count(Product.id))),1)
        self.assertEqual(self.product.onec_id,'guid-1')
    async def test_cost_import_recalculates_floor_without_overwriting_moba(self):
        self.product.onec_id='guid-1';assign_prices(self.mp,self.product,reference_prices(1000))
        xml='''<КоммерческаяИнформация><ПакетПредложений><ТипыЦен><ТипЦены><Ид>sale</Ид><Наименование>Розница</Наименование></ТипЦены><ТипЦены><Ид>cost</Ид><Наименование>Закупочная</Наименование></ТипЦены></ТипыЦен><Предложения><Предложение><Ид>guid-1</Ид><Цены><Цена><ИдТипаЦены>sale</ИдТипаЦены><ЦенаЗаЕдиницу>5</ЦенаЗаЕдиницу></Цена><Цена><ИдТипаЦены>cost</ИдТипаЦены><ЦенаЗаЕдиницу>800.01</ЦенаЗаЕдиницу></Цена></Цены></Предложение></Предложения></ПакетПредложений></КоммерческаяИнформация>'''.encode()
        await onec_exchange.import_offers(self.adapter,xml)
        self.assertEqual(self.product.purchase_price,Decimal('800.01'))
        self.assertEqual(self.mp.price,1000)
        self.assertEqual(self.mp.our_base,1200)
        self.assertEqual(self.mp.our_super_opt,921)
        feed=await onec_exchange.price_feed(self.adapter)
        self.assertEqual(feed['items'][0]['super_wholesale'],921)
        self.assertEqual(self.db.scalar(select(Stock.quantity)),8)
    async def test_unknown_price_type_is_never_treated_as_retail(self):
        self.product.onec_id='guid-1';assign_prices(self.mp,self.product,reference_prices(1000))
        xml='<Root><ТипЦены><Ид>random</Ид><Наименование>Другой</Наименование></ТипЦены><Предложение><Ид>guid-1</Ид><Цены><Цена><ИдТипаЦены>random</ИдТипаЦены><ЦенаЗаЕдиницу>1</ЦенаЗаЕдиницу></Цена></Цены></Предложение></Root>'.encode()
        await onec_exchange.import_offers(self.adapter,xml)
        self.assertEqual(self.mp.our_base,1200)
        self.assertIsNone(self.product.purchase_price)
    async def test_moba_sync_updates_own_sku_not_supplier_article(self):
        snapshot={'complete':True,'products':[{'article':'MOBA-1','name':'Дисплей Xiaomi','price':1500,'discountPrice':1000,'link':'/catalog/test/'}]}
        sync_snapshot(self.db,snapshot,apply=True);self.db.flush()
        self.assertEqual(self.mp.our_opt,1500)
        self.assertEqual(self.mp.our_base,1800)
        self.assertEqual(self.db.scalar(select(func.count(MobaPrice.id))),1)
        self.assertEqual(self.db.scalar(select(Stock.quantity)),8)
        with self.assertRaises(ValueError):sync_snapshot(self.db,{'complete':False,'products':snapshot['products']},apply=True)
    async def test_conflicting_barcode_identity_fails_instead_of_merging(self):
        self.product.onec_id='original-guid'
        with self.assertRaises(ValueError):
            await onec_exchange._find_product(self.adapter,'other-guid',self.product.sku)

    async def test_removed_cost_revokes_old_estimated_super_price(self):
        assign_prices(self.mp,self.product,reference_prices(1000,purchase_price=800))
        self.mp.link=self.mp.url=None
        self.product.purchase_price=None
        sync_snapshot(self.db,{'complete':True,'products':[{'article':'OTHER','name':'Другой товар','price':100}]},apply=True)
        self.assertIsNone(self.mp.our_super_opt)
        self.assertIsNone(self.product.price_super_opt)

class InvoiceTest(unittest.TestCase):
    def test_russian_amount_and_sum_validation(self):
        self.assertEqual(amount_words(6489),'Шесть тысяч четыреста восемьдесят девять рублей 00 копеек')
        self.assertEqual(amount_words(21001.21),'Двадцать одна тысяча один рубль 21 копейка')
        p=SimpleNamespace(name='Товар',sku='123')
        o=SimpleNamespace(id=1,created_at=datetime(2026,11,4),customer_name='Покупатель',customer_phone='',total_amount=2400,
                          items=[SimpleNamespace(product=p,price_at_time=1200,quantity=2)])
        self.assertEqual(invoice_data(o)['total'],2400)
        o.total_amount=2300
        with self.assertRaises(ValueError):invoice_data(o)

if __name__=='__main__':unittest.main()
