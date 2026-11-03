"""情绪与关系（阶段 3）。

只做三件事：
1. 按规则粗判最近的消息，只有连续多条、变化明显时才让模型归纳；
2. 把结果追加进 emotion_event（只存脱敏摘要，不存原文）；
3. 把「当前情绪」写进 persona_state，并按 TTL 自动失效。

首周默认 observe_only：面板、时间线、曲线都可用，但不向回复提示词注入。
affinity 仍按日结算，不参与「当前情绪」判断。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from astrbot.api import logger

from .refs import platform_key, session_ref

# 情绪标签保持少而稳，避免模型每次换一套说法导致曲线断裂。
LABELS = ("开心", "难过", "焦虑", "生气", "疲惫", "平静", "兴奋", "委屈", "感动", "孤独")

# 规则粗判词表：只用来决定「值不值得再调模型」，不直接当结论。
KEYWORDS: dict[str, tuple[str, ...]] = {
    "开心": ("开心", "高兴", "太好了", "哈哈", "笑死", "嘿嘿", "好耶", "棒", "爽", "喜欢", "爱了"),
    "难过": ("难过", "伤心", "哭", "失落", "崩溃", "emo", "遗憾", "心碎"),
    "焦虑": ("焦虑", "紧张", "担心", "害怕", "慌", "压力", "赶不上", "来不及", "内耗"),
    "生气": ("生气", "烦", "气死", "无语", "讨厌", "恶心", "恼火", "滚"),
    "疲惫": ("累", "困", "熬夜", "加班", "没力气", "撑不住", "疲", "想睡"),
    "兴奋": ("激动", "兴奋", "冲", "牛", "厉害", "绝了", "期待", "迫不及待"),
    "委屈": ("委屈", "冤枉", "凭什么", "欺负", "不公平"),
    "感动": ("感动", "谢谢", "温暖", "贴心", "泪目"),
    "孤独": ("孤独", "没人", "一个人", "寂寞", "好冷清"),
}

# 脱敏：不落原文，日志和时间线里只保留类别、强度、概括。
_MASK_PATTERNS = (
    (re.compile(r"1[3-9]\d{9}"), "[手机号]"),
    (re.compile(r"\b\d{6,12}\b"), "[号码]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.\w+"), "[邮箱]"),
    (re.compile(r"(?:BV[0-9A-Za-z]{10}|av\d+)"), "[视频ID]"),
    (re.compile(r"(?:密码|口令|账号|卡号)\S{0,12}"), "[敏感信息]"),
    (re.compile(r"\d+(?:\.\d+)?\s*(?:元|块|万|亿)"), "[金额]"),
)


def normalize_platform(value: Any) -> str:
    # 阶段 3 回写：与 memory/refs.py 共用一套平台归一，避免情绪事件和记忆镜像
    # 对同一个账号写出两种 platform 前缀。
    return platform_key(value)


def source_account_for(platform: str, account_id: str) -> str:
    platform = normalize_platform(platform)
    account_id = str(account_id or "").strip()
    if not platform or not account_id:
        return ""
    return f"{platform}:{account_id}"


def desensitize(text: Any, limit: int = 60) -> str:
    value = str(text or "").replace("\n", " ").strip()
    for pattern, repl in _MASK_PATTERNS:
        value = pattern.sub(repl, value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit] + ("…" if len(value) > limit else "")


def _tolerant_json(text: str) -> dict[str, Any]:
    """模型偶尔会加 ```json 或前后说明，这里尽量把第一个对象抠出来。"""
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start : end + 1])
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


class EmotionService:
    """情绪观察服务。所有异常都吞掉，坏掉也不能拖垮聊天链路。"""

    KV_RUNTIME = "emotion:runtime"
    KV_CURSOR = "emotion:cursor"
    KV_PATTERNS = "emotion:patterns"

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.store = plugin.store
        self.cfg = plugin.cfg
        self.persona_id = str(plugin.cfg.persona_id or "ling")
        self._next_sample_at = 0.0
        self._runtime: dict[str, Any] | None = None
        self._closed = False

    # ------------------------------------------------------------ 配置
    def _bool(self, key: str, default: bool) -> bool:
        return bool(self.cfg.bool(f"emotion.observe.{key}", default))

    def _int(self, key: str, default: int, low: int = 0, high: int = 10**9) -> int:
        return max(low, min(high, int(self.cfg.int(f"emotion.observe.{key}", default))))

    def enabled(self) -> bool:
        return self._bool("enabled", True)

    def mode(self) -> str:
        mode = str(self.cfg.str("emotion.observe.mode", "observe_only") or "observe_only")
        return mode if mode in {"observe_only", "inject"} else "observe_only"

    def state_ttl(self) -> int:
        return self._int("state_ttl_minutes", 120, 30, 1440) * 60

    # ------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        # 启动时只读一次运行日志，采样交给现有心跳，不额外起任务。
        self._runtime = await self._load_runtime()
        logger.info(
            f"[congling] 情绪观察就绪：mode={self.mode()} "
            f"interval={self._int('sample_interval_minutes', 30, 5, 1440)}m"
        )

    async def close(self) -> None:
        self._closed = True

    async def tick(self) -> None:
        """由主心跳每分钟调用一次，内部按采样间隔节流。"""
        if self._closed or not self.enabled():
            return
        interval = self._int("sample_interval_minutes", 30, 5, 1440) * 60
        now = time.time()
        if now < self._next_sample_at:
            return
        self._next_sample_at = now + interval
        await self.sample(manual=False)

    # ------------------------------------------------------------ 运行日志
    @staticmethod
    def _day_key(ts: float | None = None) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(ts or time.time()))

    async def _load_runtime(self) -> dict[str, Any]:
        data = await self.store.kv_get(self.KV_RUNTIME, {})
        data = data if isinstance(data, dict) else {}
        if data.get("day") != self._day_key():
            data["day"] = self._day_key()
            data["events_today"] = 0
            data["cap_truncated"] = 0
            data["model_failures"] = 0
            data["patterns_written"] = 0
        data.setdefault("samples", [])
        data.setdefault("last_sample_at", 0)
        data.setdefault("last_skip_reason", "")
        data.setdefault("last_error", "")
        data.setdefault("events_today", 0)
        data.setdefault("cap_truncated", 0)
        data.setdefault("model_failures", 0)
        data.setdefault("patterns_written", 0)
        return data

    async def runtime(self) -> dict[str, Any]:
        if self._runtime is None:
            self._runtime = await self._load_runtime()
        elif self._runtime.get("day") != self._day_key():
            self._runtime = await self._load_runtime()
        return self._runtime

    async def _save_runtime(self, **updates: Any) -> None:
        data = await self.runtime()
        data.update(updates)
        data["day"] = self._day_key()
        samples = list(data.get("samples") or [])[-9:]
        if updates.get("last_outcome"):
            samples.append(
                {
                    "at": int(updates.get("last_sample_at") or time.time()),
                    "outcome": str(updates.get("last_outcome") or ""),
                    "label": str(updates.get("last_label") or ""),
                    "person": str(updates.get("last_person") or ""),
                    "messages": int(updates.get("last_messages") or 0),
                    "model": bool(updates.get("last_model_used")),
                }
            )
        data["samples"] = samples[-10:]
        self._runtime = data
        try:
            await self.store.kv_set(self.KV_RUNTIME, data)
        except Exception as exc:
            logger.debug(f"[congling] 情绪运行日志写入失败: {exc!r}")

    async def _note_skip(self, reason: str) -> dict[str, Any]:
        await self._save_runtime(last_skip_reason=reason, last_outcome="skip")
        return {"ok": True, "skipped": reason}

    # ------------------------------------------------------------ 身份
    async def _resolve_person(self, platform: str, account_id: str, source_account: str) -> str:
        """有绑定就解析到人物，没有就退回来源账号；不把归属写死进事件。"""
        try:
            context = getattr(self.plugin, "context", None)
            meta = context.get_registered_star("astrbot_plugin_livingmemory") if context else None
            star = getattr(meta, "star_cls", None)
            identity_store = getattr(getattr(star, "initializer", None), "identity_store", None)
            if identity_store is not None:
                resolved = await identity_store.resolve_person(normalize_platform(platform), account_id)
                if resolved:
                    return str(resolved)
        except Exception as exc:
            logger.debug(f"[congling] 情绪身份解析降级: {exc!r}")
        return source_account

    @staticmethod
    def _scene_and_audience(session_kind: str, session_id: str) -> tuple[str, str]:
        if session_kind == "group":
            return f"group:{session_id}", f"origin_group:{session_id}"
        return f"private:{session_id}", "self_private"

    # ------------------------------------------------------------ 当前状态
    async def current_state(self, person_key: str) -> dict[str, Any] | None:
        key = str(person_key or "").strip()
        if not key:
            return None
        now = int(time.time())
        row = await self.store.fetch_one(
            "SELECT * FROM persona_state WHERE persona_id=? AND scope='user' "
            "AND subject_id=? AND COALESCE(emotion_expires_at,0) > ? "
            "ORDER BY emotion_updated_at DESC LIMIT 1",
            (self.persona_id, key, now),
        )
        return row or None

    async def prompt_line(self, event: Any) -> str:
        """只在 inject 模式下返回；当前状态只取未过期的一条。"""
        if not self.enabled() or self.mode() != "inject":
            return ""
        try:
            platform = normalize_platform(event.get_platform_name())
            sender = str(event.get_sender_id() or "")
            source_account = source_account_for(platform, sender)
            person = await self._resolve_person(platform, sender, source_account)
            row = await self.current_state(person)
            if not row:
                return ""
            mood = str(row.get("mood") or "").strip()
            if not mood:
                return ""
            reason = desensitize(row.get("emotion_reason") or "", 40)
            intensity = float(row.get("emotion_intensity") or 0.0)
            line = f"你此刻的心情是「{mood}」，强度约 {intensity:.0%}"
            if reason:
                line += f"，因为{reason}"
            return line + "。这是当前状态，不是过去的回忆。"
        except Exception as exc:
            logger.debug(f"[congling] 当前情绪注入失败: {exc!r}")
            return ""

    async def clear_state(self, person_key: str = "") -> int:
        """清空当前情绪。person_key 既接受 person_id，也接受 source_account。

        注意：写入时 subject_id 是解析后的人物 ID（person:xxx），如果这里只按
        原始来源账号删，会一条都删不掉；所以先把两种键都算成目标。
        """
        key = str(person_key or "").strip()
        targets: list[str] = []
        if key:
            targets.append(key)
            if not key.startswith("person:") and ":" in key:
                platform, account_id = key.split(":", 1)
                resolved = await self._resolve_person(platform, account_id, key)
                if resolved and resolved not in targets:
                    targets.append(resolved)
        count_sql = (
            "SELECT COUNT(*) FROM persona_state WHERE persona_id=? AND scope='user' "
            "AND COALESCE(emotion_expires_at,0)>0"
        )
        update_sql = (
            "UPDATE persona_state SET emotion_expires_at=0, emotion_intensity=0, "
            "emotion_reason='', source_event_id=0 WHERE persona_id=? AND scope='user'"
        )
        if not targets:
            affected = int(await self.store.scalar(count_sql, (self.persona_id,), 0) or 0)
            await self.store.execute(update_sql, (self.persona_id,))
            return affected
        affected = 0
        for target in targets:
            affected += int(await self.store.scalar(
                count_sql + " AND subject_id=?", (self.persona_id, target), 0
            ) or 0)
            await self.store.execute(
                update_sql + " AND subject_id=?", (self.persona_id, target)
            )
        return affected

    async def _write_state(
        self,
        person: str,
        *,
        label: str,
        intensity: float,
        reason: str,
        event_id: int,
        confidence: float,
        occurred_at: int,
    ) -> None:
        now = int(time.time())
        expires = now + self.state_ttl()
        row = await self.store.fetch_one(
            "SELECT subject_id FROM persona_state WHERE persona_id=? AND scope='user' AND subject_id=?",
            (self.persona_id, person),
        )
        if row:
            await self.store.execute(
                "UPDATE persona_state SET mood=?, emotion_intensity=?, emotion_reason=?, "
                "source_event_id=?, emotion_updated_at=?, emotion_expires_at=?, "
                "emotion_confidence=?, last_active=? WHERE persona_id=? AND scope='user' AND subject_id=?",
                (label, intensity, reason, event_id, now, expires, confidence, occurred_at,
                 self.persona_id, person),
            )
            return
        await self.store.execute(
            "INSERT INTO persona_state(persona_id, scope, subject_id, intimacy, mood, energy, busy, "
            "last_active, emotion_intensity, emotion_reason, source_event_id, emotion_updated_at, "
            "emotion_expires_at, emotion_confidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.persona_id, "user", person, 0.12, label, 0.7, 0, occurred_at,
             intensity, reason, event_id, now, expires, confidence),
        )

    # ------------------------------------------------------------ 规则粗判
    @staticmethod
    def _coarse(messages: list[str]) -> dict[str, Any] | None:
        counts: dict[str, int] = {}
        emotional = 0
        for text in messages:
            hit_labels = set()
            for label, words in KEYWORDS.items():
                if any(word in text for word in words):
                    hit_labels.add(label)
            if hit_labels:
                emotional += 1
                for label in hit_labels:
                    counts[label] = counts.get(label, 0) + 1
        if emotional < 2 or not counts:
            return None
        label = max(counts.items(), key=lambda item: item[1])[0]
        hits = counts[label]
        intensity = min(0.95, 0.35 + 0.1 * emotional + 0.03 * hits)
        return {"label": label, "intensity": round(intensity, 2), "hits": hits, "emotional": emotional}

    async def _model_summary(self, messages: list[str], coarse: dict[str, Any]) -> dict[str, Any] | None:
        models = getattr(self.plugin, "models", None)
        if models is None:
            return None
        body = "\n".join(f"- {desensitize(text, 120)}" for text in messages[-20:])
        prompt = (
            "下面是一个人最近一段时间的发言（已去标识）。请判断 TA 现在的情绪。\n"
            f"只能从这些标签里选一个：{'、'.join(LABELS)}。\n"
            '只输出 JSON：{"label":"标签","intensity":0到1的小数,"summary":"不超过30字的脱敏概括","confidence":0到1的小数}。\n'
            "概括里不要复述原话，不要写人名、号码、金额、链接。\n\n"
            f"发言：\n{body}"
        )
        system = "你是情绪观察器，只做分类和简短概括，不提供建议，也不评判。"
        try:
            text = await models.chat(prompt, system=system, temperature=0.2, max_tokens=220)
        except Exception as exc:
            logger.debug(f"[congling] 情绪归纳模型失败: {exc!r}")
            await self._save_runtime(model_failures=int((await self.runtime()).get("model_failures") or 0) + 1)
            return None
        data = _tolerant_json(text)
        if not data:
            return None
        label = str(data.get("label") or "").strip()
        if label not in LABELS:
            label = coarse["label"]
        summary = desensitize(data.get("summary") or "", 40)
        if not summary:
            return None
        return {
            "label": label,
            "intensity": _clamp(data.get("intensity"), 0.05, 1.0, coarse["intensity"]),
            "summary": summary,
            "confidence": _clamp(data.get("confidence"), 0.1, 1.0, 0.7),
        }

    # ------------------------------------------------------------ 采样
    async def sample(self, manual: bool = False) -> dict[str, Any]:
        if not self.enabled():
            return {"ok": False, "error": "情绪观察已关闭"}
        try:
            return await self._sample(manual)
        except Exception as exc:
            logger.warning(f"[congling] 情绪采样失败: {exc!r}")
            await self._save_runtime(last_error=repr(exc)[:200], last_outcome="error")
            return {"ok": False, "error": repr(exc)[:200]}

    async def _sample(self, manual: bool) -> dict[str, Any]:
        runtime = await self.runtime()
        now = int(time.time())
        window = self._int("window_minutes", 60, 5, 1440) * 60
        min_messages = self._int("min_messages", 5, 2, 50)
        day_start = int(time.mktime(time.strptime(self._day_key(), "%Y-%m-%d")))
        cap = self._int("max_events_per_day", 200, 10, 2000)

        if int(runtime.get("events_today") or 0) >= cap:
            await self._save_runtime(
                cap_truncated=int(runtime.get("cap_truncated") or 0) + 1,
                last_skip_reason="达到每日上限",
            )
            return {"ok": True, "skipped": "达到每日上限", "cap": cap}

        cursor = await self.store.kv_get(self.KV_CURSOR, 0)
        try:
            cursor = int(cursor or 0)
        except (TypeError, ValueError):
            cursor = 0
        # 手动采样也要求有新消息，避免空跑；首次运行只看最近一个窗口。
        start = max(now - window, cursor or now - window)
        rows = await self.store.fetch_all(
            "SELECT msg_id, session_id, session_kind, sender_id, ts, text, platform "
            "FROM msg_archive WHERE ts>? AND ts<=? AND text IS NOT NULL AND text<>'' "
            "ORDER BY ts ASC LIMIT 200",
            (start, now),
        )
        if not rows:
            await self._save_runtime(last_sample_at=now)
            return await self._note_skip("没有新消息")

        # 按人聚合，取这段窗口里说得最多的那个人。
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            account = source_account_for(row.get("platform") or "", str(row.get("sender_id") or ""))
            if not account:
                continue
            grouped.setdefault(account, []).append(row)
        if not grouped:
            await self._save_runtime(last_sample_at=now)
            return await self._note_skip("没有可识别来源")

        account, items = max(grouped.items(), key=lambda pair: len(pair[1]))
        if len(items) < min_messages:
            await self._save_runtime(last_sample_at=now, last_messages=len(items), last_person=account)
            return await self._note_skip(f"消息不足（{len(items)}/{min_messages}）")

        texts = [str(item.get("text") or "")[:500] for item in items]
        coarse = self._coarse(texts)
        if coarse is None:
            await self._save_runtime(last_sample_at=now, last_messages=len(items), last_person=account)
            return await self._note_skip("没有明显的情绪变化")

        platform = normalize_platform(items[-1].get("platform") or "")
        person = await self._resolve_person(platform, str(items[-1].get("sender_id") or ""), account)
        current = await self.current_state(person)
        if current:
            old_label = str(current.get("mood") or "")
            old_intensity = float(current.get("emotion_intensity") or 0.0)
            if old_label == coarse["label"] and abs(old_intensity - coarse["intensity"]) < 0.2:
                await self._save_runtime(last_sample_at=now, last_messages=len(items), last_person=person)
                return await self._note_skip("和当前状态相比变化不明显")

        # 先粗判，满足条件才调模型；模型失败时退回规则结论，不丢事件。
        result = await self._model_summary(texts, coarse)
        model_used = bool(result)
        result = result or {
            "label": coarse["label"],
            "intensity": coarse["intensity"],
            "summary": f"连续 {coarse['emotional']} 条消息语气偏{coarse['label']}",
            "confidence": 0.55,
        }
        label = str(result["label"])
        occurred_at = int(items[-1].get("ts") or now)
        session_kind = str(items[-1].get("session_kind") or "private")
        scene, audience = self._scene_and_audience(session_kind, str(items[-1].get("session_id") or ""))
        source_msg_id = str(items[-1].get("msg_id") or "")
        summary = desensitize(result["summary"], 60)
        event_id = await self.store.execute(
            "INSERT INTO emotion_event(persona_id, person_id, source_account, scene, label, intensity, "
            "summary, source_msg_id, audience, shareable, occurred_at, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.persona_id, person, account, scene, label, float(result["intensity"]), summary,
             source_msg_id, audience, 1 if audience == "public" else 0, occurred_at, now),
        )
        await self._write_state(
            person, label=label, intensity=float(result["intensity"]), reason=summary,
            event_id=event_id, confidence=float(result.get("confidence") or 0.55),
            occurred_at=occurred_at,
        )
        await self._maybe_write_pattern(
            person, account, label, scene, audience, occurred_at
        )
        await self.store.kv_set(self.KV_CURSOR, occurred_at)
        events_today = int(runtime.get("events_today") or 0) + 1
        await self._save_runtime(
            last_sample_at=now,
            last_skip_reason="",
            last_error="",
            last_outcome="event",
            last_label=label,
            last_person=person,
            last_messages=len(items),
            last_model_used=model_used,
            events_today=events_today,
        )
        logger.info(
            f"[congling] 情绪事件 #{event_id}：{label} {float(result['intensity']):.0%} "
            f"来源={account} 模型={model_used}"
        )
        return {
            "ok": True,
            "event_id": event_id,
            "label": label,
            "intensity": float(result["intensity"]),
            "summary": summary,
            "person": person,
            "source_account": account,
            "messages": len(items),
            "model_used": model_used,
        }

    # ------------------------------------------------------------ 外部上报
    async def ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        source_account = str(payload.get("source_account") or "").strip()
        if not source_account:
            account_id = str(payload.get("account_id") or "").strip()
            platform = str(payload.get("platform") or "bili")
            source_account = source_account_for(platform, account_id)
        if not source_account:
            return {"ok": False, "error": "缺 source_account 或 platform+account_id"}
        label = str(payload.get("label") or "").strip() or "平静"
        label = label if label in LABELS else desensitize(label, 8) or "平静"
        intensity = _clamp(payload.get("intensity"), 0.0, 1.0, 0.5)
        summary = desensitize(payload.get("summary") or "", 60) or f"外部上报：{label}"
        audience = str(payload.get("audience") or "self_private").strip()
        shareable = 1 if audience == "public" else 0
        scene = str(payload.get("scene") or "").strip()
        occurred_at = int(payload.get("occurred_at") or time.time())
        confidence = _clamp(payload.get("confidence"), 0.1, 1.0, 0.7)
        platform = normalize_platform(str(payload.get("platform") or source_account.split(":", 1)[0]))
        account_id = str(payload.get("account_id") or source_account.split(":", 1)[-1])
        person = str(payload.get("person_id") or "").strip() or await self._resolve_person(
            platform, account_id, source_account
        )
        runtime = await self.runtime()
        cap = self._int("max_events_per_day", 200, 10, 2000)
        day_start = int(time.mktime(time.strptime(self._day_key(), "%Y-%m-%d")))
        events_today = await self.store.scalar(
            "SELECT COUNT(*) FROM emotion_event WHERE persona_id=? AND occurred_at>=?",
            (self.persona_id, day_start), 0,
        )
        if int(events_today or 0) >= cap:
            await self._save_runtime(
                cap_truncated=int(runtime.get("cap_truncated") or 0) + 1,
                last_skip_reason="达到每日上限",
            )
            return {"ok": False, "error": "达到每日上限", "cap": cap}
        event_id = await self.store.execute(
            "INSERT INTO emotion_event(persona_id, person_id, source_account, scene, label, intensity, "
            "summary, source_msg_id, audience, shareable, occurred_at, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.persona_id, person, source_account, scene, label, intensity, summary,
             str(payload.get("source_msg_id") or ""), audience, shareable, occurred_at, int(time.time())),
        )
        await self._write_state(
            person, label=label, intensity=intensity, reason=summary, event_id=event_id,
            confidence=confidence, occurred_at=occurred_at,
        )
        await self._maybe_write_pattern(
            person, source_account, label, scene, audience, occurred_at
        )
        await self._save_runtime(
            last_outcome="ingest",
            last_label=label,
            last_person=person,
            events_today=int(events_today or 0) + 1,
        )
        return {"ok": True, "event_id": event_id, "person": person, "label": label}

    # ------------------------------------------------------------ 查询
    async def overview(self) -> dict[str, Any]:
        runtime = await self.runtime()
        day_start = int(time.mktime(time.strptime(self._day_key(), "%Y-%m-%d")))
        events_today = int(await self.store.scalar(
            "SELECT COUNT(*) FROM emotion_event WHERE persona_id=? AND occurred_at>=?",
            (self.persona_id, day_start), 0,
        ) or 0)
        fresh = await self.store.fetch_all(
            "SELECT * FROM persona_state WHERE persona_id=? AND scope='user' "
            "AND COALESCE(emotion_expires_at,0)>? ORDER BY emotion_updated_at DESC LIMIT 20",
            (self.persona_id, int(time.time())),
        )
        return {
            "mode": self.mode(),
            "enabled": self.enabled(),
            "observe_only": self.mode() == "observe_only",
            "states": [self._state_view(row, runtime) for row in fresh],
            "today": {
                "events": events_today,
                "cap": self._int("max_events_per_day", 200, 10, 2000),
                "runtime_events": int(runtime.get("events_today") or 0),
            },
            "runtime": self._runtime_view(runtime),
            "retention_days": self._int("retention_days", 30, 1, 365),
            "panel_curve": self._bool("panel_curve", True),
        }

    def _state_view(self, row: dict[str, Any], runtime: dict[str, Any] | None = None) -> dict[str, Any]:
        now = int(time.time())
        expires = int(row.get("emotion_expires_at") or 0)
        return {
            "person": str(row.get("subject_id") or ""),
            "mood": str(row.get("mood") or ""),
            "intensity": float(row.get("emotion_intensity") or 0.0),
            "energy": float(row.get("energy") or 0.0),
            "busy": bool(row.get("busy")),
            "reason": str(row.get("emotion_reason") or ""),
            "source_event_id": int(row.get("source_event_id") or 0),
            "updated_at": int(row.get("emotion_updated_at") or 0),
            "expires_at": expires,
            "confidence": float(row.get("emotion_confidence") or 0.0),
            "fresh": expires > now,
            "observe_only": self.mode() == "observe_only",
        }

    def _runtime_view(self, runtime: dict[str, Any]) -> dict[str, Any]:
        return {
            "last_sample_at": int(runtime.get("last_sample_at") or 0),
            "last_skip_reason": str(runtime.get("last_skip_reason") or ""),
            "last_error": str(runtime.get("last_error") or ""),
            "events_today": int(runtime.get("events_today") or 0),
            "model_failures": int(runtime.get("model_failures") or 0),
            "cap_truncated": int(runtime.get("cap_truncated") or 0),
            "patterns_written": int(runtime.get("patterns_written") or 0),
            "samples": list(runtime.get("samples") or []),
        }

    async def events(self, params: dict[str, Any]) -> dict[str, Any]:
        where = ["persona_id=?"]
        args: list[Any] = [self.persona_id]
        person = str(params.get("person") or params.get("person_id") or "").strip()
        if person:
            where.append("(person_id=? OR source_account=?)")
            args.extend([person, person])
        label = str(params.get("label") or "").strip()
        if label:
            where.append("label=?")
            args.append(label)
        for key in ("from", "to"):
            if params.get(key):
                where.append("occurred_at>=?" if key == "from" else "occurred_at<=?")
                args.append(self._parse_ts(params.get(key)))
        try:
            limit = max(1, min(200, int(params.get("limit") or 50)))
            page = max(1, int(params.get("page") or 1))
        except (TypeError, ValueError):
            limit, page = 50, 1
        sql_where = " AND ".join(where)
        total = int(await self.store.scalar(
            f"SELECT COUNT(*) FROM emotion_event WHERE {sql_where}", tuple(args), 0
        ) or 0)
        rows = await self.store.fetch_all(
            f"SELECT id, person_id, source_account, scene, label, intensity, summary, "
            f"audience, shareable, occurred_at FROM emotion_event WHERE {sql_where} "
            f"ORDER BY occurred_at DESC LIMIT ? OFFSET ?",
            tuple(args + [limit, (page - 1) * limit]),
        )
        return {
            "items": rows,
            "total": total,
            "page": page,
            "limit": limit,
            "labels": list(LABELS),
            "observe_only": self.mode() == "observe_only",
        }

    @staticmethod
    def _parse_ts(value: Any) -> int:
        text = str(value or "").strip()
        if not text:
            return 0
        try:
            return int(float(text))
        except (TypeError, ValueError):
            pass
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M"):
            try:
                return int(time.mktime(time.strptime(text, fmt)))
            except ValueError:
                continue
        return 0

    async def curve(self, params: dict[str, Any]) -> dict[str, Any]:
        range_key = str(params.get("range") or "24h").strip()
        hours = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}.get(range_key, 24)
        hourly = range_key == "24h"
        now = int(time.time())
        start = now - hours * 3600
        rows = await self.store.fetch_all(
            "SELECT label, intensity, occurred_at FROM emotion_event "
            "WHERE persona_id=? AND occurred_at>=? ORDER BY occurred_at ASC",
            (self.persona_id, start),
        )
        step = 3600 if hourly else 86400
        # 对齐到本地整点/整日，避免曲线 bucket 漂移。
        base = int(time.mktime(time.localtime(start))) // step * step
        buckets: dict[int, dict[str, Any]] = {}
        for row in rows:
            slot = int(row.get("occurred_at") or 0) // step * step
            item = buckets.setdefault(slot, {"count": 0, "sum": 0.0, "labels": {}})
            item["count"] += 1
            item["sum"] += float(row.get("intensity") or 0.0)
            label = str(row.get("label") or "")
            item["labels"][label] = item["labels"].get(label, 0) + 1
        points: list[dict[str, Any]] = []
        for slot in range(base, now + step, step):
            item = buckets.get(slot)
            labels = item["labels"] if item else {}
            points.append({
                "at": slot,
                "label": time.strftime("%m-%d %H:%M" if hourly else "%m-%d", time.localtime(slot)),
                "count": int(item["count"]) if item else 0,
                "intensity": round(item["sum"] / item["count"], 3) if item and item["count"] else 0.0,
                "top": max(labels.items(), key=lambda pair: pair[1])[0] if labels else "",
            })
        return {
            "range": range_key,
            "points": points,
            "count": len(rows),
            "mode": self.mode(),
            "observe_only": self.mode() == "observe_only",
            "panel_curve": self._bool("panel_curve", True),
        }

    async def state_payload(self, params: dict[str, Any]) -> dict[str, Any]:
        person = str(params.get("person") or params.get("person_id") or "").strip()
        source_account = str(params.get("source_account") or "").strip()
        if not person and source_account:
            platform = normalize_platform(source_account.split(":", 1)[0])
            account_id = source_account.split(":", 1)[-1]
            person = await self._resolve_person(platform, account_id, source_account)
        row = await self.current_state(person) if person else None
        affinity = None
        if person or source_account:
            affinity = await self.store.fetch_one(
                "SELECT score, stage, delta_today, updated_at FROM affinity "
                "WHERE persona_id=? AND subject_key=?",
                (self.persona_id, person or source_account),
            )
        return {
            "state": self._state_view(row) if row else None,
            "mode": self.mode(),
            "observe_only": self.mode() == "observe_only",
            "affinity": affinity or None,
            "person": person,
            "source_account": source_account,
        }

    # ------------------------------------------------------------ 稳定模式
    async def _maybe_write_pattern(
        self,
        person: str,
        source_account: str,
        label: str,
        scene: str,
        audience: str,
        occurred_at: int,
    ) -> bool:
        """稳定模式才写 LivingMemory：同人同标签 7 天内 ≥3 次，30 天内只写一次。

        原始事件仍只留在从绫；这里写的是可回忆的概括，带日期与受众，
        不写 person 归属，避免阶段 1 的绑定尚未定时把归属写死。
        """
        if not self._bool("write_pattern_to_memory", True) or not person:
            return False
        now = int(time.time())
        count = int(await self.store.scalar(
            "SELECT COUNT(*) FROM emotion_event WHERE persona_id=? AND person_id=? "
            "AND label=? AND occurred_at>=?",
            (self.persona_id, person, label, now - 7 * 86400), 0,
        ) or 0)
        if count < 3:
            return False
        memo = await self.store.kv_get(self.KV_PATTERNS, {})
        memo = memo if isinstance(memo, dict) else {}
        key = f"{person}|{label}"
        last = int(memo.get(key) or 0)
        if last and now - last < self._int("retention_days", 30, 1, 365) * 86400:
            return False
        try:
            from .bridge_livingmemory import _get_lm_engine

            engine = _get_lm_engine(self.plugin)
            if engine is None:
                return False
            platform = str(source_account or "").split(":", 1)[0]
            scene_id = str(scene or "").split(":", 1)[-1]
            kind = "group" if audience.startswith("origin_group:") else "private"
            # 阶段 3 回写：按本插件 refs 口径生成会话标识，并补 congling_persona_id /
            # congling_session_ref，否则桥接检索会因缺归属字段丢弃这条模式记忆。
            where_ref = session_ref(platform, kind, scene_id)
            date_label = time.strftime("%Y-%m-%d", time.localtime(occurred_at or now))
            content = f"{date_label}：反复出现「{label}」的情绪模式，压力大时更明显。"
            await engine.add_memory(
                content=content,
                session_id=scene_id or None,
                persona_id=self.persona_id,
                importance=0.6,
                metadata={
                    "memory_origin": "congling",
                    "memory_type": "GENERAL",
                    "congling_kind": "emotion_pattern",
                    "congling_persona_id": self.persona_id,
                    "congling_session_ref": where_ref,
                    "congling_session_kind": kind,
                    "congling_subject_key": str(person or ""),
                    "source_account": source_account,
                    "audience": audience,
                    "visible_to": "origin_group" if kind == "group" else "self_private",
                    "origin_session_ref": where_ref,
                    "emotion_label": label,
                    "pattern_date": date_label,
                },
            )
            memo[key] = now
            await self.store.kv_set(self.KV_PATTERNS, memo)
            runtime = await self.runtime()
            await self._save_runtime(
                patterns_written=int(runtime.get("patterns_written") or 0) + 1
            )
            logger.info(f"[congling] 情绪稳定模式写入 LivingMemory：{label}（{count} 次）")
            return True
        except Exception as exc:
            # 写不进去不影响事件本身，也不重试；下次采样重新判断。
            logger.debug(f"[congling] 情绪稳定模式写入失败: {exc!r}")
            return False

    # ------------------------------------------------------------ 维护
    async def purge(self) -> int:
        days = self._int("retention_days", 30, 1, 365)
        cutoff = int(time.time()) - days * 86400
        removed = await self.store.scalar(
            "SELECT COUNT(*) FROM emotion_event WHERE persona_id=? AND occurred_at<?",
            (self.persona_id, cutoff), 0,
        )
        await self.store.execute(
            "DELETE FROM emotion_event WHERE persona_id=? AND occurred_at<?",
            (self.persona_id, cutoff),
        )
        return int(removed or 0)
