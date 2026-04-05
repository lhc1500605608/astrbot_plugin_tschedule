import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_astrbot_stubs():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")

    class _Logger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def exception(self, *args, **kwargs):
            return None

    class _Filter:
        class EventMessageType:
            ALL = "all"

        @staticmethod
        def command(*args, **kwargs):
            def deco(func):
                return func

            return deco

        @staticmethod
        def event_message_type(*args, **kwargs):
            def deco(func):
                return func

            return deco

        @staticmethod
        def llm_tool(*args, **kwargs):
            def deco(func):
                return func

            return deco

    class _Star:
        def __init__(self, context):
            self.context = context

    def _register(*args, **kwargs):
        def deco(cls):
            return cls

        return deco

    event.AstrMessageEvent = object
    event.filter = _Filter
    star.Context = object
    star.Star = _Star
    star.register = _register
    api.logger = _Logger()

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.star"] = star


_install_astrbot_stubs()
from main import TschedulePlugin


class DummyContext:
    def __init__(self, data_path=None):
        self.data_path = data_path

    def get_config(self):
        return {}


class TestCollectSkillV210(unittest.TestCase):
    def setUp(self):
        self.plugin = TschedulePlugin(DummyContext(), config={})
        self.plugin.tasks = {}

    def test_validate_cron_expr_error_has_field_name(self):
        with self.assertRaises(ValueError) as e:
            self.plugin._validate_cron_expr("0 10 * 13 *")
        self.assertIn("月", str(e.exception))

    def test_parse_task_options(self):
        opts = self.plugin._parse_task_options(
            [
                "2",
                "tz=Asia/Shanghai",
                "miss=skip",
                "retry_strategy=exponential",
                "retry_interval=7",
            ]
        )
        self.assertEqual(opts["retry_times"], 2)
        self.assertEqual(opts["timezone_name"], "Asia/Shanghai")
        self.assertEqual(opts["missed_policy"], "skip")
        self.assertEqual(opts["retry_strategy"], "exponential")
        self.assertEqual(opts["retry_interval_seconds"], 7)

    def test_next_cron_time(self):
        next_dt = self.plugin._next_cron_time("*/5 * * * *", "Asia/Shanghai")
        self.assertIsNotNone(next_dt)

    def test_store_file_under_data_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin = TschedulePlugin(DummyContext(data_path=tmpdir), config={})
            self.assertTrue(str(plugin._local_store_path).startswith(tmpdir))
            self.assertEqual(plugin._local_store_path.name, "tschedule_store_v2.json")
            self.assertEqual(
                plugin._local_store_path.parent,
                Path(tmpdir) / "astrbot_plugin_tschedule",
            )


if __name__ == "__main__":
    unittest.main()
