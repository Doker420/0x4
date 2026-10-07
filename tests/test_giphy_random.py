import unittest
from types import SimpleNamespace
from unittest.mock import patch

from web import giphy


class _Response:
    def __init__(self, payload, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Stand-in for httpx.AsyncClient that records provider calls."""

    calls = []
    giphy_url = ""
    tenor_urls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, params=None, **kwargs):
        type(self).calls.append((url, dict(params or {})))
        if "giphy" in url:
            if type(self).giphy_url:
                return _Response({"data": {"images": {"downsized": {"url": type(self).giphy_url}}}})
            return _Response({"data": {}})
        return _Response({
            "results": [{"media_formats": {"gif": {"url": url_}}} for url_ in type(self).tenor_urls]
        })


class GiphyRandomTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _FakeClient.calls = []
        _FakeClient.giphy_url = ""
        _FakeClient.tenor_urls = []
        self.keys = patch.object(giphy, "_get_keys", return_value=("giphy-key", "tenor-key"))
        self.client = patch.object(giphy.httpx, "AsyncClient", _FakeClient)
        self.keys.start()
        self.client.start()

    def tearDown(self):
        self.client.stop()
        self.keys.stop()

    def _params(self, marker):
        for url, params in _FakeClient.calls:
            if marker in url:
                return params
        return None

    async def test_random_endpoint_is_used_and_not_a_fixed_first_result(self):
        _FakeClient.giphy_url = "https://cdn/one.gif"
        first = await giphy.random_gif("funny")
        self.assertEqual(first, "https://cdn/one.gif")
        # No search call is needed when the random endpoint answers.
        self.assertIsNone(self._params("/gifs/search"))
        self.assertIn("/gifs/random", _FakeClient.calls[0][0])

        _FakeClient.giphy_url = "https://cdn/two.gif"
        second = await giphy.random_gif("funny")
        self.assertEqual(second, "https://cdn/two.gif")
        self.assertNotEqual(first, second)

    async def test_avoid_list_is_respected_and_neutral_tags_vary(self):
        _FakeClient.giphy_url = "https://cdn/used.gif"
        _FakeClient.tenor_urls = ["https://cdn/fresh.gif"]
        chosen = await giphy.random_gif("", avoid=["https://cdn/used.gif"])
        self.assertEqual(chosen, "https://cdn/fresh.gif")

        tag = self._params("/gifs/random")["tag"]
        self.assertIn(tag, giphy.RANDOM_GIF_TAGS)

    async def test_tenor_offset_is_randomised_and_search_is_the_last_resort(self):
        _FakeClient.tenor_urls = ["https://cdn/a.gif", "https://cdn/b.gif"]
        chosen = await giphy.random_gif("кот")
        self.assertIn(chosen, {"https://cdn/a.gif", "https://cdn/b.gif"})
        self.assertGreaterEqual(self._params("/v2/search")["pos"], 0)

        _FakeClient.giphy_url = ""
        _FakeClient.tenor_urls = []
        with patch.object(giphy, "search_gif", new=_async_return(["https://cdn/fallback.gif"])) as fallback:
            self.assertEqual(await giphy.random_gif("пусто"), "https://cdn/fallback.gif")
        self.assertTrue(fallback.awaited)


def _async_return(value):
    from unittest.mock import AsyncMock
    return AsyncMock(return_value=value)


if __name__ == "__main__":
    unittest.main()
