import asyncio
import unittest

from sirius_api import SiriusClient


class _HangingContext:
    def __init__(self):
        self.close_started = asyncio.Event()
        self.never = asyncio.Event()

    async def new_page(self):
        return object()

    async def close(self):
        self.close_started.set()
        await self.never.wait()


class _Browser:
    def __init__(self, context):
        self.context = context

    async def new_context(self, **kwargs):
        return self.context


class SiriusLoginCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_returns_before_hanging_context_cleanup(self):
        context = _HangingContext()
        client = SiriusClient()
        client._browser = _Browser(context)
        client._ready.set()

        async def successful_login(page, email, password):
            return "token"

        client._do_login_on_page = successful_login
        self.assertEqual(await asyncio.wait_for(client.login("a@b.c", "secret"), 0.2), "token")
        await asyncio.wait_for(context.close_started.wait(), 0.2)

        for task in client._login_cleanup_tasks:
            task.cancel()
        await asyncio.gather(*client._login_cleanup_tasks, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
