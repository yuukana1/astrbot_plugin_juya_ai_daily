import asyncio
import base64
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path


try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    sys.modules["aiohttp"] = aiohttp_stub


def _install_astrbot_stubs() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_api = types.ModuleType("astrbot.api.event")
    star_api = types.ModuleType("astrbot.api.star")

    class MessageChain:
        def __init__(self):
            self.kind = ""
            self.value = ""

        def url_image(self, value):
            self.kind, self.value = "url", value
            return self

        def file_image(self, value):
            self.kind, self.value = "file", value
            return self

        def message(self, value):
            self.kind, self.value = "text", value
            return self

    class Filter:
        @staticmethod
        def command(name):
            def decorator(function):
                function.__astrbot_command_name__ = name
                return function

            return decorator

    class Star:
        def __init__(self, context=None):
            self.context = context

    class StarTools:
        @staticmethod
        def get_data_dir(_name):
            return Path(".")

    class Logger:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    event_api.filter = Filter()
    event_api.AstrMessageEvent = object
    event_api.MessageChain = MessageChain
    api.AstrBotConfig = dict
    api.logger = Logger()
    star_api.Context = object
    star_api.Star = Star
    star_api.StarTools = StarTools
    def register(name, *_args, **_kwargs):
        def decorator(cls):
            cls.__astrbot_plugin_name__ = name
            return cls

        return decorator

    star_api.register = register

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event_api,
            "astrbot.api.star": star_api,
        }
    )


_install_astrbot_stubs()
plugin_dir = Path(__file__).resolve().parents[1]
package_name = "astrbot_plugin_juya_ai_daily"
package = types.ModuleType(package_name)
package.__path__ = [str(plugin_dir)]
package.__package__ = package_name
sys.modules[package_name] = package
plugin_module = importlib.import_module("astrbot_plugin_juya_ai_daily.main")
DailyAINewsPlugin = plugin_module.DailyAINewsPlugin


class FakeEvent:
    unified_msg_origin = "test:GroupMessage:10001"

    def __init__(
        self,
        fail=False,
        message_id="manual-message-1",
        unified_msg_origin="test:GroupMessage:10001",
    ):
        self.fail = fail
        self.send_calls = 0
        self.unified_msg_origin = unified_msg_origin
        self.message_obj = types.SimpleNamespace(message_id=message_id)

    def plain_result(self, text):
        return text

    async def send(self, _chain):
        self.send_calls += 1
        if self.fail:
            raise RuntimeError("ambiguous platform error")


class FakeContext:
    def __init__(self, result=False):
        self.result = result
        self.sent_chains = []

    async def send_message(self, _umo, chain):
        self.sent_chains.append(chain)
        return self.result


class ManualDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def test_internal_plugin_name(self):
        self.assertEqual(
            DailyAINewsPlugin.__astrbot_plugin_name__,
            "astrbot_plugin_juya_ai_daily",
        )

    def test_public_commands_are_chinese(self):
        self.assertEqual(
            DailyAINewsPlugin.cmd_ainews.__astrbot_command_name__, "AI日报"
        )
        self.assertEqual(
            DailyAINewsPlugin.cmd_subscribe.__astrbot_command_name__, "AI日报订阅"
        )
        self.assertEqual(
            DailyAINewsPlugin.cmd_unsubscribe.__astrbot_command_name__, "AI日报退订"
        )
        self.assertEqual(
            DailyAINewsPlugin.cmd_status.__astrbot_command_name__, "AI日报状态"
        )

    async def test_private_chat_can_subscribe(self):
        plugin = object.__new__(DailyAINewsPlugin)
        plugin._cmd_subscriptions = set()
        saved = False

        async def save_subscriptions():
            nonlocal saved
            saved = True

        plugin._save_subscriptions = save_subscriptions
        private_umo = "test:FriendMessage:10001"
        event = FakeEvent(unified_msg_origin=private_umo)

        messages = [item async for item in plugin.cmd_subscribe(event)]

        self.assertIn(private_umo, plugin._cmd_subscriptions)
        self.assertTrue(saved)
        self.assertEqual(len(messages), 1)
        self.assertIn("当前会话", messages[0])

    def test_status_lists_group_and_private_subscription_details(self):
        plugin = object.__new__(DailyAINewsPlugin)
        plugin.config = {
            "subscribed_groups": "napcat:7788",
            "subscribed_users": "2233",
        }
        plugin._cmd_subscriptions = {
            "napcat:GroupMessage:7788",
            "napcat:FriendMessage:10001",
        }

        status = plugin._format_subscription_status(plugin._get_all_targets())

        self.assertIn("群聊订阅（1）", status)
        self.assertIn("群号 7788", status)
        self.assertIn("来源：指令、配置", status)
        self.assertIn("私聊订阅（2）", status)
        self.assertIn("用户 10001", status)
        self.assertIn("用户 2233", status)
        self.assertNotIn("指令订阅数", status)

    async def test_fetch_failure_returns_only_render_failure(self):
        plugin = object.__new__(DailyAINewsPlugin)

        async def fetch():
            return None

        plugin._fetch_rss_latest = fetch
        plugin._now = lambda: types.SimpleNamespace(
            strftime=lambda _format: "2026-08-06"
        )

        results = [item async for item in plugin.cmd_ainews(FakeEvent())]

        self.assertEqual(len(results), 1)
        self.assertIn("图片渲染失败", results[0])
        self.assertNotIn("正在从 RSS 获取", results[0])

    async def test_embedded_font_loader_accepts_ttf(self):
        plugin = object.__new__(DailyAINewsPlugin)
        font_bytes = b"\x00\x01\x00\x00test-font"

        with tempfile.TemporaryDirectory() as temp_dir:
            font_path = Path(temp_dir) / "font.ttf"
            font_path.write_bytes(font_bytes)
            plugin._embedded_font_path = lambda: font_path

            encoded = await plugin._load_render_font_data()

        self.assertEqual(base64.b64decode(encoded), font_bytes)
        self.assertEqual(plugin._font_runtime_status, "霞鹜文楷（已加载）")

    async def test_embedded_font_loader_allows_no_font_package(self):
        plugin = object.__new__(DailyAINewsPlugin)
        plugin._embedded_font_path = lambda: Path("missing-font.ttf")

        encoded = await plugin._load_render_font_data()

        self.assertEqual(encoded, "")
        self.assertEqual(
            plugin._font_runtime_status,
            "系统字体（未发现内置字体）",
        )

    async def test_single_manual_send_attempt_even_when_platform_raises(self):
        plugin = object.__new__(DailyAINewsPlugin)
        plugin.config = {"image_delivery_mode": "url"}
        event = FakeEvent(fail=True)

        sent = await plugin._send_event_image_once(
            event, "https://example.com/daily.jpg", "2026-08-06"
        )

        self.assertFalse(sent)
        self.assertEqual(event.send_calls, 1)

    async def test_render_failure_sends_only_render_notice(self):
        plugin = object.__new__(DailyAINewsPlugin)
        plugin.config = {"send_max_retries": 1, "retry_base_delay": 0}
        plugin.context = FakeContext(result=True)
        plugin._image_send_locks = {}
        plugin._recent_image_deliveries = {}
        plugin._recent_manual_image_attempts = {}

        result = await plugin._deliver_target(
            "test:GroupMessage:10001",
            {"link": "https://daily.juya.uk/issues/2026-08-06/"},
            "2026-08-06",
            None,
            "render",
        )

        self.assertTrue(result.success)
        self.assertEqual(result.mode, "render_failed_notice")
        self.assertEqual(len(plugin.context.sent_chains), 1)
        self.assertIn("渲染失败", plugin.context.sent_chains[0].value)

    async def test_send_failure_retries_image_then_sends_send_notice(self):
        plugin = object.__new__(DailyAINewsPlugin)
        plugin.config = {
            "image_delivery_mode": "url",
            "send_max_retries": 2,
            "retry_base_delay": 0,
        }
        plugin.context = FakeContext(result=False)
        async def send_message(_umo, chain):
            plugin.context.sent_chains.append(chain)
            return chain.kind == "text"

        plugin.context.send_message = send_message
        plugin._image_send_locks = {}
        plugin._recent_image_deliveries = {}
        plugin._recent_manual_image_attempts = {}

        result = await plugin._deliver_target(
            "test:GroupMessage:10001",
            {"link": "https://daily.juya.uk/issues/2026-08-06/"},
            "2026-08-06",
            "https://example.com/daily.jpg",
            "",
        )

        self.assertTrue(result.success)
        self.assertEqual(result.mode, "send_failed_notice")
        self.assertEqual(len(plugin.context.sent_chains), 3)
        self.assertIn("发送失败", plugin.context.sent_chains[-1].value)

    async def test_concurrent_duplicate_commands_emit_only_one_image(self):
        plugin = object.__new__(DailyAINewsPlugin)
        plugin.config = {
            "enable_image_render": True,
            "manual_dedupe_seconds": 60,
        }
        plugin._image_send_locks = {}
        plugin._recent_image_deliveries = {}
        plugin._recent_manual_image_attempts = {}
        article = {
            "title": "2026-08-06",
            "link": "https://daily.juya.uk/issues/2026-08-06/",
        }

        async def fetch():
            return article

        async def render(*_args):
            return "https://example.com/daily.jpg"

        send_calls = 0

        async def send_once(*_args):
            nonlocal send_calls
            send_calls += 1
            await asyncio.sleep(0.02)
            return True

        plugin._fetch_rss_latest = fetch
        plugin._parse_article_date = lambda _article: "2026-08-06"
        plugin._get_cached_image = lambda _date: None
        plugin._render_news_image = render
        plugin._send_event_image_once = send_once

        async def consume(event):
            return [item async for item in plugin.cmd_ainews(event)]

        first, second = await asyncio.gather(
            consume(FakeEvent()), consume(FakeEvent())
        )

        self.assertEqual(send_calls, 1)
        self.assertEqual(first + second, [])

    async def test_scheduled_delivery_does_not_block_manual_command(self):
        plugin = object.__new__(DailyAINewsPlugin)
        plugin.config = {
            "enable_image_render": True,
            "manual_dedupe_seconds": 60,
        }
        plugin._image_send_locks = {}
        plugin._recent_image_deliveries = {}
        plugin._recent_manual_image_attempts = {}
        article = {
            "title": "2026-08-06",
            "link": "https://daily.juya.uk/issues/2026-08-06/",
        }

        async def fetch():
            return article

        send_calls = 0

        async def send_once(*_args):
            nonlocal send_calls
            send_calls += 1
            return True

        plugin._fetch_rss_latest = fetch
        plugin._parse_article_date = lambda _article: "2026-08-06"
        plugin._get_cached_image = lambda _date: "https://example.com/daily.jpg"
        plugin._send_event_image_once = send_once

        scheduled_key = plugin._image_delivery_key(
            "scheduled", article["link"], FakeEvent.unified_msg_origin
        )
        plugin._remember_image_sent(scheduled_key)

        first_results = [
            item
            async for item in plugin.cmd_ainews(
                FakeEvent(message_id="new-manual-command")
            )
        ]
        second_results = [
            item
            async for item in plugin.cmd_ainews(
                FakeEvent(message_id="another-manual-command")
            )
        ]

        self.assertEqual(send_calls, 2)
        self.assertEqual(first_results + second_results, [])


if __name__ == "__main__":
    unittest.main()
