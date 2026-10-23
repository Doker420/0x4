"""Human-in-the-loop Telegram alert formatting and delivery tests."""

from __future__ import annotations

import unittest

from airdrop.models import Airdrop
from airdrop.notify import Notifier


class _FakeBot:
    def __init__(self) -> None:
        self.sent = []

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class NotifyTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_alert_has_manual_source_button_and_caution(self) -> None:
        notifier = Notifier(token="test-token", admin_ids=[123])
        fake = _FakeBot()
        notifier._bot = fake
        item = Airdrop(
            name="Public project",
            url="https://official.example.org/quest",
            source="rss",
            summary="Check the official campaign.",
            score=7.2,
        )

        delivered = await notifier.new_drops([item])

        self.assertEqual(delivered, 1)
        args, message = fake.sent[0]
        text = args[1]
        self.assertIn("ручная проверка", text)
        self.assertIn("не подтверждение награды", text)
        markup = message["reply_markup"]
        button = markup.inline_keyboard[0][0]
        self.assertEqual(button.url, item.url)
        self.assertIn("ручной проверки", button.text)

    async def test_non_http_source_does_not_create_open_link_button(self) -> None:
        notifier = Notifier(token="test-token", admin_ids=[123])
        fake = _FakeBot()
        notifier._bot = fake
        await notifier.new_drops([Airdrop(name="Unsafe", url="javascript:alert(1)")])
        self.assertIsNone(fake.sent[0][1]["reply_markup"])

    async def test_no_configuration_does_not_report_delivery(self) -> None:
        notifier = Notifier(token="", admin_ids=[])
        delivered = await notifier.new_drops([Airdrop(name="Queued", url="https://example.org")])
        self.assertEqual(delivered, 0)
