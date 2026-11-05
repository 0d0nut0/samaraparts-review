"""Снимок публичного каталога Moba без изменения БД и запуска браузера.

python -m scripts.fetch_moba_catalog --output /path/to/snapshot.json
Ответы кэшируются по страницам. Повторный запуск продолжает тот же снимок;
для нового снимка используйте новый --cache-dir.
"""
import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from app.config import settings

from app.services.moba_parser import (
    MOBA_BASE, category_page_count, extract_category_tree, extract_products_from_html,
)


class CatalogFetcher:
    def __init__(self, cache_dir: Path, interval: float = 0.4):
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.interval = max(interval, 0.3)
        self.lock = asyncio.Lock()
        self.next_request = 0.0
        self.completed = 0
        self.errors = []

    async def request(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        """Retry temporary network failures without ever applying a partial snapshot."""
        for attempt in range(3):
            async with self.lock:
                loop = asyncio.get_running_loop()
                await asyncio.sleep(max(0, self.next_request - loop.time()))
                self.next_request = loop.time() + self.interval
            try:
                response = await client.get(url)
            except httpx.TransportError:
                if attempt == 2:
                    raise
                await asyncio.sleep(3 * (attempt + 1))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    try:
                        pause = float(response.headers.get('Retry-After', 3 * (attempt + 1)))
                    except ValueError:
                        pause = 6
                    await asyncio.sleep(min(30, max(3, pause)))
                    continue
            response.raise_for_status()
            return response
        raise RuntimeError(f'Unable to fetch {url}')

    async def page(self, client: httpx.AsyncClient, path: str, number: int) -> dict:
        parts = urlsplit(path)
        if parts.netloc or not parts.path.startswith('/catalog/') or parts.query:
            raise ValueError(f'Invalid catalog path: {path}')
        url = MOBA_BASE + path + (f'?page={number}' if number > 1 else '')
        key = hashlib.sha256(url.encode()).hexdigest()[:24]
        cached = self.cache_dir / f'{key}.json'
        if cached.exists():
            self.completed += 1
            return json.loads(cached.read_text())
        response = await self.request(client, url)
        products = extract_products_from_html(response.text)
        result = {'url': url, 'fetched_at': datetime.now(timezone.utc).isoformat(),
                  'sha256': hashlib.sha256(response.content).hexdigest(),
                  'page_count': category_page_count(response.text, path), 'products': products}
        cached.write_text(json.dumps(result, ensure_ascii=False))
        self.completed += 1
        if self.completed % 20 == 0:
            print(f'pages={self.completed}', flush=True)
        return result

    async def category(self, client, category):
        path = category['linkUrl']
        first = await self.page(client, path, 1)
        pages = first['page_count']
        print(f"{category['name']}: pages={pages}, first={len(first['products'])}", flush=True)
        records = list(first['products'])
        # Small batches bound memory and connection use; the shared limiter also
        # limits request starts across categories.
        for start in range(2, pages + 1, 3):
            batch = await asyncio.gather(*(self.page(client, path, n)
                                          for n in range(start, min(start + 3, pages + 1))))
            for record in batch:
                if not record['products']:
                    self.errors.append({'url': record['url'], 'error': 'empty advertised page'})
                records.extend(record['products'])
        unique = {}
        for product in records:
            product = dict(product)
            product['_category'] = category['name']
            product['_category_path'] = path
            product['_parsed_url'] = MOBA_BASE + path
            unique.setdefault(product['article'], product)
        print(f"{category['name']}: products={len(unique)}", flush=True)
        return list(unique.values())


async def collect(output: Path, cache_dir: Path):
    fetcher = CatalogFetcher(cache_dir)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, proxy=settings.MOBA_HTTP_PROXY or None,
                                 limits=httpx.Limits(max_connections=3, max_keepalive_connections=3)) as client:
        home = await fetcher.request(client, MOBA_BASE + '/')
        roots = [c for c in extract_category_tree(home.text)
                 if c.get('linkUrl', '').startswith('/catalog/') and c.get('linkUrl') != '/catalog/']
        if not roots:
            raise RuntimeError('Moba category tree is missing; snapshot not written')
        products = {}
        for category in roots:
            for product in await fetcher.category(client, category):
                products.setdefault(product['article'], product)
            # Keep a recoverable partial result, explicitly marked incomplete.
            partial = {'source': MOBA_BASE, 'complete': False,
                       'fetched_at': datetime.now(timezone.utc).isoformat(),
                       'categories_finished': category['name'], 'products': list(products.values()),
                       'errors': fetcher.errors}
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix('.partial.json')
            temporary.write_text(json.dumps(partial, ensure_ascii=False))
        result = {'source': MOBA_BASE, 'complete': not fetcher.errors,
                  'fetched_at': datetime.now(timezone.utc).isoformat(),
                  'categories': len(roots), 'pages': fetcher.completed,
                  'products': list(products.values()), 'errors': fetcher.errors}
        output.write_text(json.dumps(result, ensure_ascii=False))
        print(json.dumps({k: v for k, v in result.items() if k != 'products'}, ensure_ascii=False), flush=True)
        print(f'products={len(products)} output={output}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache-dir', type=Path)
    args = parser.parse_args()
    asyncio.run(collect(args.output, args.cache_dir or args.output.parent / 'moba-pages'))


if __name__ == '__main__':
    main()
