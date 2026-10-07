import unittest

from web.manager import parse_proxy, test_proxy_connection, validate_session_name


class ManagerInputTests(unittest.IsolatedAsyncioTestCase):
    def test_proxy_formats(self):
        url = parse_proxy("socks5://alice:p%40ss@proxy.example:1080")
        self.assertEqual(url["hostname"], "proxy.example")
        self.assertEqual(url["username"], "alice")
        self.assertEqual(url["password"], "p@ss")
        self.assertEqual(url["port"], 1080)

        classic = parse_proxy("proxy.example:8000:alice:secret")
        self.assertEqual(classic["scheme"], "socks5")
        self.assertEqual(classic["port"], 8000)
        self.assertEqual(parse_proxy('{"scheme":"http","hostname":"localhost","port":8080}')['scheme'], "http")

    def test_proxy_parser_rejects_code_and_invalid_ports(self):
        with self.assertRaises(ValueError):
            parse_proxy("__import__('os').system('echo unsafe')")
        with self.assertRaises(ValueError):
            parse_proxy("socks5://proxy.example:70000")

    def test_session_name_is_path_safe(self):
        self.assertEqual(validate_session_name("agent_2"), "agent_2")
        for value in ("../outside", "a/b", "", "x" * 65):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_session_name(value)

    async def test_empty_proxy_test_returns_helpful_error(self):
        result = await test_proxy_connection("")
        self.assertFalse(result["ok"])
        self.assertIn("прокси", result["error"].lower())


if __name__ == "__main__":
    unittest.main()
