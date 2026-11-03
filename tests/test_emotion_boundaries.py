"""情绪边界回归：真实服务/存储方法，临时 SQLite，合成事件，禁止联网。"""

from __future__ import annotations

import asyncio
import ast
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest


ROOT = Path(os.environ.get("EMOTION_PLUGIN_ROOT", Path(__file__).resolve().parents[1]))
MODULE = Path(os.environ.get("EMOTION_MODULE_PATH", ROOT / "memory/emotion.py"))
PAGE = Path(os.environ.get("EMOTION_PAGE_PATH", ROOT / "page_api/emotion.py"))
BILI = Path(os.environ.get(
    "EMOTION_BILI_PATH", Path(__file__).resolve().parents[2] / "BiliBot/core/emotion_bridge.py"
))
PACKAGE = "_emotion_boundary_test"


def _deny_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        raise AssertionError("情绪测试禁止联网")


sys.addaudithook(_deny_network)
if "astrbot.api" not in sys.modules:
    sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(**{
        name: lambda *a, **kw: None
        for name in ("debug", "info", "warning", "error", "exception")
    })
    sys.modules["astrbot.api"] = api
for name, directory in (
    (PACKAGE, ROOT), (PACKAGE + ".core", ROOT / "core"),
    (PACKAGE + ".memory", ROOT / "memory"),
):
    package = types.ModuleType(name)
    package.__path__ = [str(directory)]
    sys.modules[name] = package


def _load(name, path):
    spec = importlib.util.spec_from_file_location(PACKAGE + "." + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


Config = _load("core.config", ROOT / "core/config.py").Config
Store = _load("core.store", ROOT / "core/store.py").Store
emotion = _load("memory.emotion", MODULE)
bridge = types.ModuleType(PACKAGE + ".memory.bridge_livingmemory")
bridge._get_lm_engine = lambda plugin: plugin.engine
sys.modules[bridge.__name__] = bridge

NOW = 1793740000
PERSON = "person:synthetic"
ACCOUNT = "qq:synthetic-user"
PRIVATE = "private:synthetic-user"
GROUP = "group:synthetic-group"
GROUP_AUDIENCE = "origin_group:synthetic-group"


class Models:
    def __init__(self):
        self.calls = 0
        self.fail = False

    async def chat(self, *args, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("合成模型失败")
        return json.dumps({
            "label": "焦虑", "intensity": 0.75,
            "summary": "synthetic-summary", "confidence": 0.8,
        })


class Engine:
    def __init__(self):
        self.writes = []

    async def add_memory(self, **kwargs):
        self.writes.append(kwargs)
        return "synthetic-memory"


class Identity:
    def __init__(self):
        self.active = True

    async def resolve_person(self, platform, account):
        return PERSON if self.active else ""


class Event:
    def __init__(self, group="", platform="aiocqhttp", sender="synthetic-user"):
        self.group, self.platform, self.sender = group, platform, sender

    def get_platform_name(self):
        return self.platform

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return self.group

    def is_private_chat(self):
        return not self.group


class EmotionBoundaries(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="emotion-boundary-")
        self.store = Store(Path(self.tmp.name) / "isolated.db")
        await self.store.open()
        self.clock = NOW
        self.original_time = emotion.time
        emotion.time = types.SimpleNamespace(
            time=lambda: self.clock, localtime=time.localtime, strftime=time.strftime,
            strptime=time.strptime, mktime=time.mktime,
        )
        self.cfg = Config({"emotion": {"observe": {
            "enabled": True, "mode": "observe_only", "state_ttl_minutes": 120,
            "retention_days": 30, "max_events_per_day": 200,
            "window_minutes": 60, "min_messages": 5,
            "write_pattern_to_memory": True,
        }}})
        self.models, self.engine, self.identity = Models(), Engine(), Identity()
        lm = types.SimpleNamespace(initializer=types.SimpleNamespace(identity_store=self.identity))
        context = types.SimpleNamespace(
            get_registered_star=lambda name: types.SimpleNamespace(star_cls=lm),
        )
        self.plugin = types.SimpleNamespace(
            cfg=self.cfg, store=self.store, models=self.models, context=context,
            engine=self.engine,
        )
        self.service = emotion.EmotionService(self.plugin)
        self.plugin.emotion = self.service

    async def asyncTearDown(self):
        emotion.time = self.original_time
        await self.store.close()
        self.tmp.cleanup()

    def inject_mode(self):
        self.cfg.override("emotion.observe.mode", "inject")

    async def ingest(self, **overrides):
        payload = {
            "source_account": ACCOUNT, "label": "焦虑", "intensity": 0.75,
            "summary": "synthetic-summary", "scene": PRIVATE,
            "audience": "self_private", "occurred_at": self.clock,
        }
        payload.update(overrides)
        result = await self.service.ingest(payload)
        self.assertTrue(result["ok"])
        return result

    async def state(self, **context):
        return (await self.service.state_payload({"person": PERSON, **context}))["state"]

    async def raw_state(self):
        return await self.store.fetch_one(
            "SELECT * FROM persona_state WHERE persona_id=? AND subject_id=?",
            (self.service.persona_id, PERSON),
        )

    async def archive(self, kind, session, text, offset):
        await self.store.execute(
            "INSERT INTO msg_archive(msg_id,session_id,session_kind,sender_id,ts,text,platform) "
            "VALUES(?,?,?,?,?,?,?)",
            (f"synthetic-{offset}", session, kind, "synthetic-user",
             NOW + offset, text, "aiocqhttp"),
        )

    async def test_private_state_not_returned_to_public_context(self):
        await self.ingest()
        self.assertIsNone(await self.state(audience="public", scene="video_comment"))

    async def test_private_state_not_in_group_prompt(self):
        await self.ingest()
        self.inject_mode()
        self.assertEqual(await self.service.prompt_line(Event("synthetic-group")), "")

    async def test_expired_ingest_remains_history_not_current(self):
        await self.ingest(occurred_at=NOW - 7201)
        self.assertIsNone(await self.state(audience="self_private"))
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM emotion_event"), 1)

    async def test_delayed_event_uses_remaining_ttl(self):
        await self.ingest(occurred_at=NOW - 600)
        row = await self.raw_state()
        self.assertEqual(row["emotion_expires_at"], NOW + 6600)
        self.assertEqual(row["emotion_updated_at"], NOW - 600)

    async def test_older_event_cannot_replace_newer_current(self):
        first = await self.ingest()
        await self.ingest(occurred_at=NOW - 60, label="疲惫")
        self.assertEqual((await self.raw_state())["source_event_id"], first["event_id"])

    async def test_private_events_do_not_count_for_public_pattern(self):
        await self.ingest()
        await self.ingest()
        await self.ingest(audience="public", scene="video_comment")
        self.assertEqual(len(self.engine.writes), 0)

    async def test_other_group_does_not_count_for_pattern(self):
        await self.ingest(audience=GROUP_AUDIENCE, scene=GROUP)
        await self.ingest(audience=GROUP_AUDIENCE, scene=GROUP)
        await self.ingest(audience="origin_group:other-group", scene="group:other-group")
        self.assertEqual(len(self.engine.writes), 0)

    async def test_private_sample_not_promoted_by_last_group_message(self):
        for offset in range(-5, -1):
            await self.archive("private", "synthetic-user", "合成测试：焦虑", offset)
        await self.archive("group", "synthetic-group", "synthetic-neutral", -1)
        result = await self.service.sample()
        self.assertIn("skipped", result)
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM emotion_event"), 0)
        self.assertEqual(self.models.calls, 0)

    async def test_observe_only_defaults_unchanged(self):
        await self.ingest()
        self.assertEqual(self.service.mode(), "observe_only")
        self.assertEqual(self.service.state_ttl(), 7200)
        view = await self.service.overview()
        self.assertEqual(view["retention_days"], 30)
        self.assertEqual(view["today"]["cap"], 200)
        for event in (Event(), Event("synthetic-group"), Event(platform="bili")):
            self.assertEqual(await self.service.prompt_line(event), "")

    async def test_private_inject_positive_control(self):
        await self.ingest()
        self.inject_mode()
        self.assertIn("synthetic-summary", await self.service.prompt_line(Event()))

    async def test_group_inject_only_same_group_and_platform(self):
        await self.ingest(audience=GROUP_AUDIENCE, scene=GROUP)
        self.inject_mode()
        self.assertTrue(await self.service.prompt_line(Event("synthetic-group")))
        self.assertEqual(await self.service.prompt_line(Event("other-group")), "")
        self.assertEqual(await self.service.prompt_line(Event()), "")
        self.assertIsNone(await self.state(
            audience=GROUP_AUDIENCE, scene=GROUP, source_account="bili:synthetic-user",
        ))
        self.assertIsNone(await self.state(audience=GROUP_AUDIENCE, scene=GROUP))

    async def test_public_state_has_explicit_positive_control(self):
        await self.ingest(audience="public", scene="video_comment", source_account="bili:synthetic-user")
        self.assertIsNotNone(await self.state(audience="public", scene="video_comment"))
        self.assertIsNone(await self.state(audience="self_private"))
        self.assertIsNone(await self.state(audience=GROUP_AUDIENCE))

    async def test_context_missing_or_query_admin_is_not_authorization(self):
        await self.ingest()
        self.assertIsNone(await self.service.current_state(PERSON))
        self.assertIsNone(await self.state(admin=True))
        self.assertIsNone(await self.state(audience="unknown"))

    async def test_admin_handler_keeps_explicit_privileged_read(self):
        await self.ingest()
        # 执行真实 handler 方法体，仅替换 HTTP 参数读取；不冒充 HTTP 鉴权验收。
        tree = ast.parse(PAGE.read_text())
        method = next(
            item for cls in tree.body if isinstance(cls, ast.ClassDef)
            for item in cls.body
            if isinstance(item, ast.AsyncFunctionDef) and item.name == "emotion_state"
        )

        async def params():
            return {"person": PERSON}

        namespace = {"Any": object, "_params": params, "_ok": lambda data: data,
                     "_err": lambda value: {"error": value}}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(PAGE), "exec"), namespace)
        result = await namespace["emotion_state"](types.SimpleNamespace(_emotion=lambda: self.service))
        self.assertIsNotNone(result["state"])

    async def test_revoked_binding_does_not_read_former_person_state(self):
        await self.ingest()
        self.identity.active = False
        self.inject_mode()
        self.assertEqual(await self.service.prompt_line(Event()), "")
        self.assertIsNone((await self.service.state_payload(
            {"source_account": ACCOUNT, "audience": "self_private"}
        ))["state"])

    async def test_unknown_event_platform_is_not_private_by_default(self):
        await self.ingest()
        self.inject_mode()
        self.assertEqual(await self.service.prompt_line(Event(platform="bili")), "")
        self.assertEqual(await self.service.prompt_line(Event(platform="unknown")), "")
        malformed = Event()
        malformed.is_private_chat = lambda: False
        self.assertEqual(await self.service.prompt_line(malformed), "")

    async def test_inconsistent_source_scene_is_not_readable_or_pattern_evidence(self):
        for _ in range(3):
            await self.ingest(audience="public", scene=PRIVATE)
        self.assertIsNone(await self.state(audience="public"))
        self.assertEqual(self.engine.writes, [])

    async def test_orphan_source_event_is_not_current(self):
        await self.ingest()
        await self.store.execute("DELETE FROM emotion_event")
        self.assertIsNone(await self.state(audience="self_private"))
        self.assertEqual((await self.service.overview())["states"], [])

    async def test_exact_ttl_boundary(self):
        await self.ingest()
        self.clock = NOW + 7199
        self.assertIsNotNone(await self.state(audience="self_private"))
        self.clock = NOW + 7200
        self.assertIsNone(await self.state(audience="self_private"))
        self.assertEqual((await self.service.overview())["states"], [])
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM emotion_event"), 1)

    async def test_zero_future_and_expired_events_stay_out_of_current(self):
        for occurred_at in (0, NOW + 1, NOW - 7200):
            await self.ingest(occurred_at=occurred_at)
            self.assertIsNone(await self.raw_state())
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM emotion_event"), 3)

    async def test_legacy_processing_time_cannot_revive_expired_state(self):
        await self.ingest()
        await self.store.execute("UPDATE emotion_event SET occurred_at=?", (NOW - 7201,))
        self.assertIsNone(await self.state(audience="self_private"))
        self.assertEqual((await self.service.overview())["states"], [])

    async def test_legacy_processing_time_remaining_ttl_is_clamped(self):
        await self.ingest()
        await self.store.execute("UPDATE emotion_event SET occurred_at=?", (NOW - 600,))
        self.assertEqual((await self.state(audience="self_private"))["expires_at"], NOW + 6600)
        self.assertEqual((await self.service.overview())["states"][0]["expires_at"], NOW + 6600)

    async def test_state_upsert_preserves_non_emotion_fields(self):
        await self.ingest()
        await self.store.execute(
            "UPDATE persona_state SET intimacy=0.91,energy=0.4,busy=1,agenda_json='synthetic-agenda'"
        )
        await self.ingest(label="疲惫")
        row = await self.raw_state()
        self.assertEqual((row["intimacy"], row["energy"], row["busy"], row["agenda_json"]),
                         (0.91, 0.4, 1, "synthetic-agenda"))

    async def test_concurrent_state_writes_keep_latest_event_time(self):
        self.cfg.override("emotion.observe.write_pattern_to_memory", False)
        await asyncio.gather(*(self.ingest(occurred_at=NOW - offset) for offset in range(8)))
        self.assertEqual((await self.raw_state())["emotion_updated_at"], NOW)

    async def test_same_scene_sampling_and_model_stub(self):
        for offset in range(-6, -1):
            await self.archive("private", "synthetic-user", "合成测试：焦虑", offset)
        await self.archive("group", "synthetic-group", "synthetic-neutral", -1)
        result = await self.service.sample()
        self.assertTrue(result["ok"])
        self.assertTrue(result["model_used"])
        row = await self.store.fetch_one("SELECT * FROM emotion_event")
        self.assertEqual((row["audience"], row["scene"], row["shareable"]),
                         ("self_private", PRIVATE, 0))
        self.assertEqual(result["messages"], 5)
        self.assertEqual(self.models.calls, 1)

    async def test_private_state_does_not_suppress_group_sample(self):
        await self.ingest(intensity=0.95)
        for offset in range(-5, 0):
            await self.archive("group", "synthetic-group", "合成测试：焦虑", offset)
        result = await self.service.sample()
        self.assertIn("event_id", result)

    async def test_model_failure_keeps_rule_event(self):
        self.models.fail = True
        for offset in range(-5, 0):
            await self.archive("private", "synthetic-user", "合成测试：焦虑", offset)
        result = await self.service.sample()
        self.assertTrue(result["ok"])
        self.assertFalse(result["model_used"])
        self.assertEqual(self.models.calls, 1)

    async def test_empty_or_unknown_scene_does_not_call_model(self):
        self.assertIn("skipped", await self.service.sample())
        for offset in range(-5, 0):
            await self.archive("unknown", "synthetic-group", "合成测试：焦虑", offset)
        self.assertIn("skipped", await self.service.sample())
        self.assertEqual(self.models.calls, 0)

    async def test_same_scene_pattern_and_independent_cooldown(self):
        for _ in range(4):
            await self.ingest()
        self.assertEqual(len(self.engine.writes), 1)
        self.assertEqual(self.engine.writes[0]["metadata"]["audience"], "self_private")
        for _ in range(3):
            await self.ingest(audience="public", scene="video_comment")
        self.assertEqual(len(self.engine.writes), 2)
        self.assertEqual(self.engine.writes[1]["metadata"]["audience"], "public")

    async def test_accounts_and_private_scenes_do_not_pool_pattern_evidence(self):
        for _ in range(2):
            await self.ingest()
        await self.ingest(source_account="bili:synthetic-user")
        await self.ingest(scene="private:other-private")
        self.assertEqual(self.engine.writes, [])

    async def test_pattern_seven_day_window_excludes_future_and_stale_trigger(self):
        await self.ingest(occurred_at=NOW - 7 * 86400 - 1)
        await self.ingest(occurred_at=NOW + 1)
        await self.ingest()
        self.assertEqual(self.engine.writes, [])
        await self.ingest(occurred_at=NOW - 7 * 86400)
        self.assertEqual(self.engine.writes, [])
        await self.ingest()
        self.assertEqual(len(self.engine.writes), 1)

    async def test_legacy_cooldown_retained_without_broadening_new_keys(self):
        await self.store.kv_set(self.service.KV_PATTERNS, {f"{PERSON}|焦虑": NOW})
        for _ in range(3):
            await self.ingest()
        self.assertEqual(self.engine.writes, [])
        self.clock = NOW + 30 * 86400
        for _ in range(3):
            await self.ingest()
        self.assertEqual(len(self.engine.writes), 1)

    async def test_retention_and_daily_cap_unchanged(self):
        await self.ingest(occurred_at=NOW - 30 * 86400 - 1)
        await self.ingest(occurred_at=NOW - 30 * 86400)
        self.assertEqual(await self.service.purge(), 1)
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM emotion_event"), 1)
        await self.store.executemany(
            "INSERT INTO emotion_event(persona_id,occurred_at) VALUES(?,?)",
            [(self.service.persona_id, NOW)] * 200,
        )
        result = await self.service.ingest({"source_account": ACCOUNT, "occurred_at": NOW})
        self.assertFalse(result["ok"])
        self.assertEqual(result["cap"], 200)
        await self.service._save_runtime(events_today=200)
        self.assertIn("skipped", await self.service.sample())

    async def test_clear_state_keeps_history(self):
        await self.ingest()
        self.assertEqual(await self.service.clear_state(ACCOUNT), 1)
        self.assertIsNone(await self.state(audience="self_private"))
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM emotion_event"), 1)

    async def test_real_bili_report_and_affinity_reconcile_remain_dual_read(self):
        # 使用真实 Bili 桥接；文件写入接收器仅收集结果，不改作息或本地好感。
        bili_config = types.ModuleType(PACKAGE + ".core.config")
        bili_config.DATA_DIR = self.tmp.name
        original = sys.modules[PACKAGE + ".core.config"]
        sys.modules[PACKAGE + ".core.config"] = bili_config
        try:
            bili = _load("core.emotion_bridge", BILI)
        finally:
            sys.modules[PACKAGE + ".core.config"] = original
        saved = []
        plugin = types.SimpleNamespace(
            context=types.SimpleNamespace(
                get_registered_star=lambda name: types.SimpleNamespace(star_cls=self.plugin)
            ),
            _affection={"synthetic-user": 50.0},
            _save_json=lambda path, payload: saved.append(payload),
        )
        self.assertEqual(bili._bridge_config(self.service)[0], "dual_read")
        await bili._report(plugin, "synthetic-user", "synthetic-name", 1, "dm")
        await bili._report(plugin, "synthetic-user", "synthetic-name", 1, "comment")
        rows = await self.store.fetch_all("SELECT audience,shareable FROM emotion_event ORDER BY id")
        self.assertEqual([(r["audience"], r["shareable"]) for r in rows],
                         [("self_private", 0), ("public", 1)])
        await self.store.execute(
            "INSERT INTO affinity(persona_id,subject_key,score) VALUES(?,?,?)",
            (self.service.persona_id, PERSON, 300),
        )
        await bili.run_daily_reconcile(plugin)
        self.assertEqual(plugin._affection, {"synthetic-user": 50.0})
        self.assertEqual(saved[0]["read_mode"], "dual_read")
        self.assertEqual(saved[0]["items"][0]["remote"], 30.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
