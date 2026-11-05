"""Печатный счёт по сохранённым ценам заказа, без изменения склада и проведения продажи."""
from decimal import Decimal, ROUND_HALF_UP
from app.shop_info import SHOP

BANK = dict(account='40802810654740021014', name='ПОВОЛЖСКИЙ БАНК ПАО СБЕРБАНК',
            bik='043601607', correspondent='30101810200000000607')
ONES=('','один','два','три','четыре','пять','шесть','семь','восемь','девять')
TEENS=('десять','одиннадцать','двенадцать','тринадцать','четырнадцать','пятнадцать','шестнадцать','семнадцать','восемнадцать','девятнадцать')
TENS=('','','двадцать','тридцать','сорок','пятьдесят','шестьдесят','семьдесят','восемьдесят','девяносто')
HUNDREDS=('','сто','двести','триста','четыреста','пятьсот','шестьсот','семьсот','восемьсот','девятьсот')


def _form(n, forms):
    return forms[2] if 11 <= n % 100 <= 14 else forms[0] if n % 10 == 1 else forms[1] if 2 <= n % 10 <= 4 else forms[2]


def _triple(n, female=False):
    words=[HUNDREDS[n//100]]
    n%=100
    if 10 <= n < 20:words.append(TEENS[n-10])
    else:
        words.append(TENS[n//10])
        units=('','одна','две',*ONES[3:]) if female else ONES
        words.append(units[n%10])
    return ' '.join(w for w in words if w)


def amount_words(value):
    value=Decimal(value).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    if value < 0 or value >= Decimal('1000000000000'):
        raise ValueError('Сумма вне диапазона')
    rub=int(value);kop=int((value-rub)*100);words=[]
    for divisor, forms, female in ((1000000000,('миллиард','миллиарда','миллиардов'),False),
                                  (1000000,('миллион','миллиона','миллионов'),False),
                                  (1000,('тысяча','тысячи','тысяч'),True)):
        n=rub//divisor%1000
        if n:words.extend([_triple(n,female),_form(n,forms)])
    words.append(_triple(rub%1000) if rub%1000 else ('ноль' if rub==0 else ''))
    words.extend([_form(rub,('рубль','рубля','рублей')),f'{kop:02}',_form(kop,('копейка','копейки','копеек'))])
    return ' '.join(w for w in words if w).capitalize()


def money(value):
    number=Decimal(value)
    return (f'{number:,.0f}' if number==number.to_integral_value() else f'{number:,.2f}').replace(',',' ').replace('.',',')


def invoice_data(order):
    rows=[]
    for i,it in enumerate(order.items,1):
        price=Decimal(it.price_at_time);amount=price*it.quantity
        rows.append(dict(number=i,name=it.product.name,code=it.product.sku,quantity=it.quantity,
                         unit='шт',price=price,before=amount,discount=Decimal(0),amount=amount))
    total=sum((r['amount'] for r in rows),Decimal(0))
    if total != Decimal(order.total_amount):
        raise ValueError('Сумма строк не совпадает с сохранённой суммой заказа')
    return dict(number=str(order.id), date=order.created_at.strftime('%d.%m.%Y'), seller=SHOP,
                buyer=order.customer_name, phone=order.customer_phone, basis=f'Заказ № {order.id}',
                rows=rows,total=total,discount=Decimal(0),quantity=sum(r['quantity'] for r in rows),
                words=amount_words(total),bank=BANK,demo=False)
