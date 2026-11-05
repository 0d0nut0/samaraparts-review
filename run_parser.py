"""Публичный HTTP-парсер Moba и пересчёт нашего каталога.

По умолчанию — проверка без изменения БД. Для рабочего обновления: --apply.
Снимки и отчёты сохраняются в var/prices. Незавершённая загрузка не применяется.
"""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import select
from app.database import SessionLocal
from app.models import Product, MobaPrice
from app.services.catalog_sync import sync_snapshot
from scripts.fetch_moba_catalog import collect
from scripts.sync_catalog_prices import read_catalog
from app.config import settings


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--apply', action='store_true')
    args=parser.parse_args()
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder=Path('var/prices');folder.mkdir(parents=True,exist_ok=True)
    snapshot=args.snapshot or folder/f'moba-{stamp}.json'
    if not args.snapshot:
        asyncio.run(collect(snapshot, folder/f'pages-{stamp}'))
    data=json.loads(snapshot.read_text())
    aliases={p['sku']:p for p in read_catalog(Path('catalog.xlsx'))} if Path('catalog.xlsx').exists() else {}
    with SessionLocal() as db:
        # Backup before any mutation, including all four public price fields.
        backup={}
        for table, rows, fields in (
            ('products',db.scalars(select(Product)),('id','price_moba','price_base','price_card','price_opt','price_super_opt')),
            ('prices',db.scalars(select(MobaPrice)),('id','sku','name','price','discount_price','our_base','our_card','our_opt','our_super_opt','url','link','parsed_at'))):
            backup[table]=[{f:getattr(r,f) for f in fields} for r in rows]
        if args.apply:
            (folder/f'before-{stamp}.json').write_text(json.dumps(backup,ensure_ascii=False,default=str))
        report=sync_snapshot(db,data,apply=args.apply,aliases=aliases)
        path=folder/f'result-{stamp}.json'
        path.write_text(json.dumps(report,ensure_ascii=False))
        if args.apply:
            db.commit()
            latest=Path(settings.MOBA_SNAPSHOT_PATH)
            latest.parent.mkdir(parents=True,exist_ok=True)
            temporary=latest.with_suffix('.tmp')
            temporary.write_text(json.dumps(data,ensure_ascii=False))
            temporary.replace(latest)
        else: db.rollback()
        print(json.dumps({'applied':args.apply,'summary':report['summary'],'report':str(path)},ensure_ascii=False))

if __name__=='__main__':main()
