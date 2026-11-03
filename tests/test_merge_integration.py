"""Offline regression for the emotion / reply-ownership merge boundary."""
import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from test_wake_persona_flow import main_hook


class MergeIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.extras = {
            "_at_guard_reply_owner": "at_guard",
            "congling:persona_token": "current",
        }
        self.result = types.SimpleNamespace(
            chain=[types.SimpleNamespace(text="reply")],
            is_llm_result=lambda: True,
        )
        self.event = types.SimpleNamespace(
            get_extra=self.extras.get,
            get_result=lambda: self.result,
            get_group_id=lambda: "group",
            get_sender_id=lambda: "user",
            unified_msg_origin="qq:GroupMessage:group",
        )
        self.plugin = types.SimpleNamespace(
            enabled_for=lambda _: True,
            diag=None,
            store=types.SimpleNamespace(kv_set=AsyncMock()),
            persona=types.SimpleNamespace(
                resolve=AsyncMock(return_value={"token": "current"})),
            guard=types.SimpleNamespace(clean_chain=Mock()),
            composer=types.SimpleNamespace(decorate=AsyncMock()),
            delayer=types.SimpleNamespace(mode=lambda: "standalone", patch=Mock()),
            night=types.SimpleNamespace(
                wake_idle=object(), take_sleepy_line=Mock(return_value="sleepy")),
        )

    async def test_owner_keeps_sleepy_line_without_duplicate_format_or_delay(self):
        await main_hook("express_hook")(self.plugin, self.event)
        self.assertEqual([part.text for part in self.result.chain], ["sleepy", "reply"])
        self.plugin.guard.clean_chain.assert_not_called()
        self.plugin.composer.decorate.assert_not_awaited()
        self.plugin.delayer.patch.assert_not_called()

    async def test_owner_absent_preserves_local_format_and_delay(self):
        self.extras.pop("_at_guard_reply_owner")
        await main_hook("express_hook")(self.plugin, self.event)
        self.plugin.composer.decorate.assert_awaited_once_with(self.event, self.result)
        self.plugin.delayer.patch.assert_called_once_with(self.event, session_kind="group")

    async def test_changed_persona_blocks_even_when_at_guard_owns_reply(self):
        self.plugin.persona.resolve.return_value = {"token": "changed"}
        await main_hook("express_hook")(self.plugin, self.event)
        self.assertEqual(self.result.chain, [])
        self.plugin.night.take_sleepy_line.assert_not_called()

    async def test_failed_wake_blocks_even_when_at_guard_owns_reply(self):
        self.extras["congling:wake_blocked"] = True
        await main_hook("express_hook")(self.plugin, self.event)
        self.assertEqual(self.result.chain, [])
        self.plugin.composer.decorate.assert_not_awaited()

    async def test_non_llm_owner_does_not_clean_chain(self):
        self.result.is_llm_result = lambda: False
        await main_hook("express_hook")(self.plugin, self.event)
        self.plugin.guard.clean_chain.assert_not_called()
        self.assertEqual([part.text for part in self.result.chain], ["reply"])

    async def test_streaming_owner_does_not_clean_chain(self):
        name = "astrbot.core.message.message_event_result"
        module = types.ModuleType(name)
        module.ResultContentType = types.SimpleNamespace(STREAMING_RESULT="stream")
        from unittest.mock import patch
        self.result.result_content_type = "stream"
        with patch.dict(sys.modules, {name: module}):
            await main_hook("express_hook")(self.plugin, self.event)
        self.plugin.guard.clean_chain.assert_not_called()
        self.assertEqual([part.text for part in self.result.chain], ["reply"])

    async def test_emotion_loop_remains_independent_of_night_heartbeat(self):
        loop = main_hook("_emotion_loop")
        fake_asyncio = types.SimpleNamespace(
            sleep=AsyncMock(), CancelledError=asyncio.CancelledError)
        loop.__globals__["asyncio"] = fake_asyncio
        plugin = types.SimpleNamespace(
            emotion=types.SimpleNamespace(tick=AsyncMock(side_effect=asyncio.CancelledError)),
            cfg=types.SimpleNamespace(bool=lambda *_: False),
        )
        await loop(plugin)
        plugin.emotion.tick.assert_awaited_once()
        fake_asyncio.sleep.assert_awaited_once_with(45)

    async def test_missing_emotion_exits_without_starting_timer(self):
        loop = main_hook("_emotion_loop")
        fake_asyncio = types.SimpleNamespace(
            sleep=AsyncMock(), CancelledError=asyncio.CancelledError)
        loop.__globals__["asyncio"] = fake_asyncio
        await loop(types.SimpleNamespace(emotion=None))
        fake_asyncio.sleep.assert_not_awaited()

    def test_desktop_loads_merged_features_and_retains_mobile_entry(self):
        root = Path(__file__).resolve().parents[1]
        from html.parser import HTMLParser

        class Page(HTMLParser):
            def __init__(self):
                super().__init__()
                self.scripts = []
                self.views = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "script" and attrs.get("src", "").startswith("./"):
                    self.scripts.append(attrs["src"].split("?")[0])
                if "data-view" in attrs:
                    self.views.append(attrs["data-view"])

        html = (root / "pages/fromling/index.html").read_text()
        page = Page()
        page.feed(html)
        self.assertIn("emotion", page.views)
        self.assertIn("bindings", page.views)
        self.assertIn("./app.js", page.scripts)
        self.assertNotIn("./app.1029c.js", page.scripts)
        for src in page.scripts:
            self.assertTrue((root / "pages/fromling" / src).is_file(), src)
        self.assertIn('q.get("desk") === "1"', html)
        self.assertIn('href="./mobile.html"', html)
        self.assertNotIn("layout-toggle.js", html)


if __name__ == "__main__":
    unittest.main()
