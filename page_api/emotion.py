"""情绪与关系页面接口（阶段 3）。

入口只在从绫详情页的「情绪与关系」Tab；这里不注册任何全局入口。
所有返回沿用页面约定：{ok, data, error}。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api.web import request

_LABELS = ("开心", "难过", "焦虑", "生气", "疲惫", "平静", "兴奋", "委屈", "感动", "孤独")


def _ok(data: Any = None) -> dict[str, Any]:
    return {"status": "ok", "ok": True, "data": data, "ts": int(time.time())}


def _err(message: str) -> dict[str, Any]:
    return {"status": "error", "ok": False, "message": message, "error": message,
            "data": {}, "ts": int(time.time())}


async def _body() -> dict[str, Any]:
    payload = await request.json(default={})
    return payload if isinstance(payload, dict) else {}


async def _params() -> dict[str, Any]:
    data: dict[str, Any] = {}
    try:
        raw = getattr(request, "query", None)
        if callable(raw):
            raw = raw()
        if raw is not None and callable(getattr(raw, "items", None)):
            for key, value in raw.items():
                data[str(key)] = value
    except Exception:
        pass
    try:
        payload = await request.json(default={})
        if isinstance(payload, dict):
            data.update(payload)
    except Exception:
        pass
    return data


class EmotionMixin:
    """PageApi 的情绪分栏。"""

    def _emotion(self) -> Any:
        return getattr(self.plugin, "emotion", None)

    async def emotion_overview(self) -> dict[str, Any]:
        try:
            service = self._emotion()
            if service is None:
                return _err("情绪模块未就绪")
            return _ok(await service.overview())
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def emotion_events(self) -> dict[str, Any]:
        try:
            service = self._emotion()
            if service is None:
                return _err("情绪模块未就绪")
            return _ok(await service.events(await _params()))
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def emotion_curve(self) -> dict[str, Any]:
        try:
            service = self._emotion()
            if service is None:
                return _err("情绪模块未就绪")
            return _ok(await service.curve(await _params()))
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def emotion_config_dispatch(self) -> dict[str, Any]:
        """同一路径 GET 读、POST 存，和方案里的接口口径保持一致。"""
        method = str(getattr(request, "method", "") or "").upper()
        if method == "POST":
            return await self.emotion_config_save()
        return await self.emotion_config_get()

    async def emotion_config_get(self) -> dict[str, Any]:
        try:
            service = self._emotion()
            if service is None:
                return _err("情绪模块未就绪")
            snapshot = self.cfg.snapshot()
            observe = snapshot.get("emotion", {}) if isinstance(snapshot, dict) else {}
            bili = (observe.get("bili") if isinstance(observe, dict) else {}) or {}
            observe = (observe.get("observe") if isinstance(observe, dict) else {}) or {}
            return _ok({"observe": observe, "bili": bili, "labels": list(_LABELS)})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def emotion_config_save(self) -> dict[str, Any]:
        """保存前逐项校验范围；越界直接报错，不静默截断。"""
        try:
            service = self._emotion()
            if service is None:
                return _err("情绪模块未就绪")
            body = await _body()
            observe = body.get("observe") if isinstance(body.get("observe"), dict) else {}
            bili = body.get("bili") if isinstance(body.get("bili"), dict) else {}
            ranges = {
                "sample_interval_minutes": (5, 1440),
                "min_messages": (2, 50),
                "window_minutes": (5, 1440),
                "state_ttl_minutes": (30, 1440),
                "retention_days": (1, 365),
                "max_events_per_day": (10, 2000),
            }
            cleaned: dict[str, Any] = {}
            for key, (low, high) in ranges.items():
                if key not in observe:
                    continue
                try:
                    value = int(float(observe[key]))
                except (TypeError, ValueError):
                    return _err(f"{key} 必须是数字")
                if not low <= value <= high:
                    return _err(f"{key} 超出范围（{low}~{high}）")
                cleaned[key] = value
            for key in ("enabled", "write_pattern_to_memory", "panel_curve"):
                if key in observe:
                    cleaned[key] = bool(observe[key])
            if "mode" in observe:
                mode = str(observe["mode"])
                if mode not in {"observe_only", "inject"}:
                    return _err("mode 只能是 observe_only 或 inject")
                cleaned["mode"] = mode
            bili_cleaned: dict[str, Any] = {}
            if "read_mode" in bili:
                read_mode = str(bili["read_mode"])
                if read_mode not in {"dual_read", "from_congling", "off"}:
                    return _err("bili.read_mode 只能是 dual_read / from_congling / off")
                bili_cleaned["read_mode"] = read_mode
            if "write_local" in bili:
                bili_cleaned["write_local"] = bool(bili["write_local"])
            if "reconcile_warn_threshold" in bili:
                try:
                    threshold = float(bili["reconcile_warn_threshold"])
                except (TypeError, ValueError):
                    return _err("对账告警阈值必须是数字")
                if not 1 <= threshold <= 100:
                    return _err("对账告警阈值超出范围（1~100）")
                bili_cleaned["reconcile_warn_threshold"] = threshold

            nested = {"emotion": {}}
            if cleaned:
                nested["emotion"]["observe"] = cleaned
            if bili_cleaned:
                nested["emotion"]["bili"] = bili_cleaned
            if not nested["emotion"]:
                return _err("没有要保存的字段")
            saved = await self._persist(nested)
            return _ok({"saved": saved, "observe": cleaned, "bili": bili_cleaned})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def emotion_sample(self) -> dict[str, Any]:
        try:
            service = self._emotion()
            if service is None:
                return _err("情绪模块未就绪")
            return _ok(await service.sample(manual=True))
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def emotion_state_clear(self) -> dict[str, Any]:
        try:
            service = self._emotion()
            if service is None:
                return _err("情绪模块未就绪")
            body = await _body()
            person = str(body.get("person") or body.get("person_id") or body.get("source_account") or "")
            affected = await service.clear_state(person)
            return _ok({"cleared": True, "affected": affected, "person": person})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def emotion_ingest(self) -> dict[str, Any]:
        """BiliBot 上报入口：只收脱敏摘要，不收原文。"""
        try:
            service = self._emotion()
            if service is None:
                return _err("情绪模块未就绪")
            result = await service.ingest(await _body())
            return _ok(result) if result.get("ok") else _err(str(result.get("error") or "上报失败"))
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def emotion_state(self) -> dict[str, Any]:
        try:
            service = self._emotion()
            if service is None:
                return _err("情绪模块未就绪")
            # 此路由属于鉴权管理面板；回复读取不得从请求参数开启管理可见性。
            return _ok(await service.state_payload(await _params(), admin=True))
        except Exception as exc:
            return _err(repr(exc)[:200])
