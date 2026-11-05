import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from scripts.fetch_moba_catalog import CatalogFetcher


class MobaNetworkTest(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_response_retried_but_bounded(self):
        attempts = 0

        def respond(request):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise httpx.RemoteProtocolError('incomplete body', request=request)
            return httpx.Response(200, text='complete')

        with tempfile.TemporaryDirectory() as folder, patch(
            'scripts.fetch_moba_catalog.asyncio.sleep', new_callable=AsyncMock
        ):
            fetcher = CatalogFetcher(Path(folder))
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                result = await fetcher.request(client, 'https://moba.ru/')
            self.assertEqual(result.text, 'complete')
            self.assertEqual(attempts, 3)

    async def test_denied_request_not_retried_and_failed_page_not_cached(self):
        attempts = 0

        def respond(request):
            nonlocal attempts
            attempts += 1
            return httpx.Response(403)

        with tempfile.TemporaryDirectory() as folder, patch(
            'scripts.fetch_moba_catalog.asyncio.sleep', new_callable=AsyncMock
        ):
            fetcher = CatalogFetcher(Path(folder))
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                with self.assertRaises(httpx.HTTPStatusError):
                    await fetcher.page(client, '/catalog/test/', 1)
            self.assertEqual(attempts, 1)
            self.assertEqual(list(Path(folder).iterdir()), [])

    async def test_persistent_connection_failure_stops_after_three_attempts(self):
        attempts = 0

        def respond(request):
            nonlocal attempts
            attempts += 1
            raise httpx.ConnectError('unavailable', request=request)

        with tempfile.TemporaryDirectory() as folder, patch(
            'scripts.fetch_moba_catalog.asyncio.sleep', new_callable=AsyncMock
        ):
            fetcher = CatalogFetcher(Path(folder))
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                with self.assertRaises(httpx.ConnectError):
                    await fetcher.page(client, '/catalog/test/', 1)
            self.assertEqual(attempts, 3)
            self.assertEqual(list(Path(folder).iterdir()), [])
