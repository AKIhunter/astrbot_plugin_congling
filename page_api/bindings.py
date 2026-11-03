"""绑定管理页面接口（阶段 3）。

入口只在从绫详情页的「绑定管理」一级 Tab；不注册 Bot Manager / 集成平台入口。
所有返回沿用页面约定：{ok, data, error}。

数据源是 LivingMemory 的 person_binding / binding_audit / binding_candidate：
- 后台上传、页面手改：免逐条审核，但强制写审计；
- 自动识别：先进入候选队列，默认必须逐条人工审核；
- auto_approve 默认关闭，开启必须写理由并确认风险。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import time
from typing import Any
from uuid import uuid4

from astrbot.api import logger
from astrbot.api.web import request

LIVINGMEMORY_PLUGIN = "astrbot_plugin_livingmemory"

_COLUMN_ALIASES: dict[str, set[str]] = {
    "person_id": {"person_id", "person", "personid", "人物", "人物id"},
    "platform": {"platform", "平台"},
    "account_id": {"account_id", "account", "accountid", "账号", "账号id", "uid"},
    "display_name": {"display_name", "displayname", "name", "显示名", "昵称"},
    "evidence_type": {"evidence_type", "evidencetype", "依据类型", "证据类型"},
    "evidence_ref": {"evidence_ref", "evidenceref", "依据", "证据"},
    "note": {"note", "备注"},
}


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


def _number(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _boolean(body: dict[str, Any], key: str, default: bool = False) -> bool:
    value = body.get(key, default)
    if type(value) is not bool:
        raise ValueError(f"{key} 必须是布尔值")
    return value


def _normalize_header(value: Any) -> str:
    return str(value or "").strip().casefold().replace(" ", "").replace("-", "_")


def _canonical_row(raw: dict[str, Any], index: int) -> tuple[dict[str, Any], str]:
    """把上传行按列名别名归一；缺关键列时返回错误说明（容错但不猜）。"""
    values: dict[str, Any] = {}
    for key, value in raw.items():
        normalized = _normalize_header(key)
        for target, aliases in _COLUMN_ALIASES.items():
            if normalized in aliases:
                if target in values:
                    return {}, f"第 {index} 行有重复列 {target}"
                if isinstance(value, (dict, list, bool)):
                    return {}, f"第 {index} 行的 {target} 必须是文本或数字"
                values[target] = value
                break
    row = {
        "person_id": str(values.get("person_id") or "").strip(),
        "platform": str(values.get("platform") or "").strip().lower(),
        "account_id": str(values.get("account_id") or "").strip(),
        "display_name": str(values.get("display_name") or "").strip(),
        "evidence_type": str(values.get("evidence_type") or "").strip(),
        "evidence_ref": str(values.get("evidence_ref") or "").strip(),
        "note": str(values.get("note") or "").strip(),
    }
    missing = [key for key in ("person_id", "platform", "account_id") if not row[key]]
    if missing:
        return row, f"第 {index} 行缺少 {', '.join(missing)}"
    return row, ""


def _parse_csv(content: str) -> tuple[list[dict[str, Any]], list[str]]:
    sample = content.splitlines()[0] if content.splitlines() else ""
    delimiter = ","
    for candidate in (",", "\t", ";", "|"):
        if candidate in sample:
            delimiter = candidate
            break
    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter, strict=True)
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        headers = [_normalize_header(value) for value in reader.fieldnames or []]
        if len(headers) != len(set(headers)):
            return [], ["CSV 有重复列名"]
        for index, raw in enumerate(reader, start=2):
            if None in raw:
                errors.append(f"第 {index} 行的列数多于表头")
                continue
            row, error = _canonical_row(dict(raw or {}), index)
            if error:
                errors.append(error)
                continue
            rows.append(row)
    except csv.Error:
        errors.append("CSV 格式错误，请检查引号和列数")
    return rows, errors


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """在解析阶段拒绝重复键，避免字段被静默覆盖后参与 replace。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 有重复键：{key}")
        result[key] = value
    return result


def _parse_upload(content: str, filename: str = "") -> tuple[list[dict[str, Any]], list[str]]:
    """兼容 JSON 数组 / {"rows": []} / {"bindings": []} 与 CSV，列名支持中英文别名。"""
    text = str(content or "").lstrip("\ufeff")
    if not text.strip():
        return [], ["文件为空"]
    lower_name = str(filename or "").lower()
    looks_json = lower_name.endswith(".json") or text.lstrip()[:1] in {"[", "{"}
    if looks_json:
        try:
            payload = json.loads(text, object_pairs_hook=_unique_json_object)
        except ValueError as exc:
            return [], [f"JSON 解析失败：{exc}"]
        if isinstance(payload, dict):
            payload = next((payload[key] for key in ("rows", "bindings", "items") if key in payload), None)
        if not isinstance(payload, list):
            return [], ["JSON 顶层需要是数组，或包含 rows/bindings 数组"]
        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        for index, raw in enumerate(payload, start=1):
            if not isinstance(raw, dict):
                errors.append(f"第 {index} 行不是对象")
                continue
            row, error = _canonical_row(raw, index)
            if error:
                errors.append(error)
                continue
            rows.append(row)
        return rows, errors
    return _parse_csv(text)


def _content_hash(content: str) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def _duplicate_accounts(rows: list[dict[str, Any]]) -> bool:
    return len({(row["platform"], row["account_id"]) for row in rows}) != len(rows)


def _preview_token(
    rows: list[dict[str, Any]], mode: str, active: list[dict[str, Any]], file_hash: str = "",
) -> str:
    payload = json.dumps(
        {
            "mode": mode,
            "file_hash": file_hash,
            "rows": sorted(rows, key=lambda row: (row.get("platform", ""), row.get("account_id", ""))),
            "active": sorted(active, key=lambda item: str(item.get("binding_id") or "")),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BindingMixin:
    """PageApi 的绑定管理分栏。"""

    def _lm_plugin(self) -> Any:
        """拿到 LivingMemory 插件实例；拿不到返回 None。"""
        context = getattr(self.plugin, "context", None)
        if context is None:
            return None
        star = None
        try:
            meta = context.get_registered_star(LIVINGMEMORY_PLUGIN)
            if meta is not None and getattr(meta, "activated", True) is not False:
                star = getattr(meta, "star_cls", None)
        except Exception:
            star = None
        if star is None:
            try:
                for meta in context.get_all_stars() or []:
                    name = str(getattr(meta, "root_dir_name", "") or getattr(meta, "name", "") or "")
                    if name == LIVINGMEMORY_PLUGIN and getattr(meta, "activated", True):
                        star = getattr(meta, "star_cls", None)
                        break
            except Exception:
                star = None
        if star is None or not hasattr(star, "list_binding_records"):
            return None
        return star

    def _require_lm(self) -> Any:
        lm = self._lm_plugin()
        if lm is None:
            raise RuntimeError("LivingMemory 未安装或未就绪，绑定管理不可用")
        return lm

    async def bindings_overview(self) -> dict[str, Any]:
        try:
            lm = self._require_lm()
            active = await lm.list_binding_records(include_history=False)
            candidates = await lm.list_binding_candidates(status="pending", limit=200)
            settings = await lm.get_binding_settings()
            platforms = sorted({str(item.get("platform") or "") for item in active if item.get("platform")})
            persons = sorted({str(item.get("person_id") or "") for item in active if item.get("person_id")})
            return _ok({
                "active_count": len(active),
                "pending_count": len(candidates),
                "platforms": platforms,
                "person_count": len(persons),
                "settings": settings,
            })
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def bindings_list(self) -> dict[str, Any]:
        try:
            lm = self._require_lm()
            params = await _params()
            status = str(params.get("status") or "").strip()
            include_history = bool(params.get("include_history")) or status == "all"
            if status == "all":
                status = ""
            items = await lm.list_binding_records(
                person_id=str(params.get("person_id") or "").strip(),
                platform=str(params.get("platform") or "").strip(),
                account_id=str(params.get("account_id") or "").strip(),
                status=status,
                include_history=include_history,
            )
            return _ok({"items": items, "count": len(items)})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def bindings_preview(self) -> dict[str, Any]:
        """变更预览：受影响记忆条数、场景分布、前 10 条样本。只读。"""
        try:
            lm = self._require_lm()
            params = await _params()
            source_account = str(params.get("source_account") or "").strip()
            person_id = str(params.get("person_id") or "").strip()
            if not source_account and not person_id:
                return _err("请提供 source_account 或 person_id")
            data = await lm.preview_binding_change(
                source_account=source_account,
                person_id=person_id,
                sample_limit=_number(params.get("sample_limit"), 10, 1, 50),
            )
            return _ok(data)
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def bindings_revoke(self) -> dict[str, Any]:
        try:
            lm = self._require_lm()
            body = await _body()
            binding_id = str(body.get("binding_id") or "").strip()
            operator = str(body.get("operator") or "").strip()
            reason = str(body.get("reason") or "").strip()
            if not binding_id:
                return _err("缺 binding_id")
            if not operator:
                return _err("请填写操作人")
            if not reason:
                return _err("请填写撤销依据")
            changed = await lm.revoke_binding_change(
                binding_id, operator=operator,
                reviewer=str(body.get("reviewer") or "").strip(),
                reason=reason, request_id=uuid4().hex,
            )
            return _ok({"changed": bool(changed), "binding_id": binding_id})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def bindings_save(self) -> dict[str, Any]:
        """手动新增/更正：账号保持不变，人物变化由存储层版本化。"""
        try:
            lm = self._require_lm()
            body = await _body()
            row, error = _canonical_row(body, 1)
            if error:
                return _err(error)
            operator = str(body.get("operator") or "").strip()
            reason = str(body.get("reason") or "").strip()
            if not operator or not reason:
                return _err("请填写操作人和变更依据")
            expected = body.get("expected_binding", {})
            if not isinstance(expected, dict):
                return _err("expected_binding 必须是绑定快照")
            result = await lm.apply_binding_change(
                **row, operator=operator, reviewer=str(body.get("reviewer") or "").strip(),
                reason=reason, channel="page", request_id=uuid4().hex,
                expected_binding=expected,
            )
            return _ok(result)
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def bindings_rollback(self) -> dict[str, Any]:
        try:
            lm = self._require_lm()
            body = await _body()
            binding_id = str(body.get("binding_id") or "").strip()
            operator = str(body.get("operator") or "").strip()
            reason = str(body.get("reason") or "").strip()
            to_version = _number(body.get("to_version"), 0, 0, 1_000_000)
            if not binding_id or not to_version:
                return _err("缺 binding_id 或 to_version")
            if not operator:
                return _err("请填写操作人")
            if not reason:
                return _err("请填写回滚依据")
            result = await lm.rollback_binding_change(
                binding_id, to_version, operator=operator,
                reviewer=str(body.get("reviewer") or "").strip(),
                reason=reason, request_id=uuid4().hex,
            )
            if not result.get("binding"):
                return _err("没有这个版本，或绑定不存在")
            return _ok(result)
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def bindings_audit(self) -> dict[str, Any]:
        try:
            lm = self._require_lm()
            params = await _params()
            items = await lm.binding_audit_records(
                binding_id=str(params.get("binding_id") or "").strip(),
                action=str(params.get("action") or "").strip(),
                limit=_number(params.get("limit"), 100, 1, 500),
                offset=_number(params.get("offset"), 0, 0, 1_000_000),
            )
            return _ok({"items": items, "count": len(items)})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def bindings_candidates(self) -> dict[str, Any]:
        try:
            lm = self._require_lm()
            params = await _params()
            items = await lm.list_binding_candidates(
                status=str(params.get("status") or "pending").strip(),
                platform=str(params.get("platform") or "").strip(),
                person_id=str(params.get("person_id") or "").strip(),
                limit=_number(params.get("limit"), 100, 1, 500),
            )
            return _ok({"items": items, "count": len(items)})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def bindings_candidates_review(self) -> dict[str, Any]:
        """自动识别候选逐条审核：通过才写正式绑定，驳回也留审计。"""
        try:
            lm = self._require_lm()
            body = await _body()
            candidate_id = str(body.get("candidate_id") or "").strip()
            reviewer = str(body.get("reviewer") or "").strip()
            reason = str(body.get("reason") or "").strip()
            if not candidate_id:
                return _err("缺 candidate_id")
            if not reviewer:
                return _err("请填写审核人")
            if not reason:
                return _err("请填写审核依据")
            result = await lm.review_binding_candidate(
                candidate_id, approve=_boolean(body, "approve"),
                reviewer=reviewer, reason=reason, request_id=uuid4().hex,
                expected_candidate=body.get("expected_candidate"),
            )
            if result is None:
                return _err("候选不存在或已审核")
            return _ok(result)
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def bindings_settings(self) -> dict[str, Any]:
        try:
            lm = self._require_lm()
            return _ok(await lm.get_binding_settings())
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def bindings_settings_save(self) -> dict[str, Any]:
        try:
            lm = self._require_lm()
            body = await _body()
            enabled = _boolean(body, "auto_approve")
            reason = str(body.get("reason") or "").strip()
            operator = str(body.get("operator") or "").strip()
            if enabled and not reason:
                return _err("开启 auto_approve 必须写明理由")
            risk_ack = _boolean(body, "risk_ack")
            if enabled and not risk_ack:
                return _err("开启 auto_approve 需要先确认风险提示")
            if not operator:
                return _err("请填写操作人")
            data = await lm.set_binding_auto_approve(
                enabled, reason=reason, operator=operator,
                reviewer=str(body.get("reviewer") or "").strip(),
                risk_ack=risk_ack, request_id=uuid4().hex,
            )
            return _ok(data)
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def bindings_upload_preview(self) -> dict[str, Any]:
        """上传预览：新增/更新/冲突/将撤销数量；replace 模式确认后再执行。"""
        try:
            lm = self._require_lm()
            body = await _body()
            mode = str(body.get("mode") or "merge").strip().lower()
            if mode not in {"merge", "replace"}:
                return _err("mode 必须是 merge 或 replace")
            content = str(body.get("content") or "")
            rows, errors = _parse_upload(content, str(body.get("filename") or ""))
            if not rows:
                return _err("没有可用的绑定行" + (f"：{errors[0]}" if errors else ""))
            duplicate = _duplicate_accounts(rows)
            if duplicate:
                errors.append("文件内账号重复，请合并后重新上传")
            active = await lm.list_binding_records(include_history=False)
            active_map = {
                (str(item.get("platform") or ""), str(item.get("account_id") or "")): item
                for item in active
            }
            created, updated, unchanged, conflicts = [], [], [], []
            seen: set[tuple[str, str]] = set()
            for row in rows:
                key = (row["platform"], row["account_id"])
                seen.add(key)
                current = active_map.get(key)
                if current is None:
                    created.append(row)
                elif str(current.get("person_id") or "") != row["person_id"]:
                    conflicts.append({
                        **row,
                        "current_person_id": str(current.get("person_id") or ""),
                        "reason": "同一账号指向不同人物，需人工审",
                    })
                elif (
                    str(current.get("display_name") or "") != row["display_name"]
                    or str(current.get("evidence_type") or "") != row["evidence_type"]
                    or str(current.get("evidence_ref") or "") != row["evidence_ref"]
                    or str(current.get("note") or "") != row["note"]
                ):
                    updated.append(row)
                else:
                    unchanged.append(row)
            revoke_items = []
            if mode == "replace":
                revoke_items = [
                    {
                        "binding_id": str(item.get("binding_id") or ""),
                        "platform": str(item.get("platform") or ""),
                        "account_id": str(item.get("account_id") or ""),
                        "person_id": str(item.get("person_id") or ""),
                        "display_name": str(item.get("display_name") or ""),
                    }
                    for key, item in active_map.items() if key not in seen
                ]
            file_hash = _content_hash(content)
            token = _preview_token(rows, mode, active, file_hash)
            return _ok({
                "mode": mode,
                "rows": len(rows),
                "created": len(created),
                "updated": len(updated),
                "unchanged": len(unchanged),
                "conflicts": conflicts,
                "revoke_count": len(revoke_items),
                "revoke_items": revoke_items[:50],
                "errors": errors,
                "can_apply": not duplicate and not (mode == "replace" and (errors or conflicts)),
                "preview_token": token,
                "file_hash": file_hash,
                "warnings": (
                    ["replace 模式会撤销文件中未出现的现有绑定，请确认预览后再执行"]
                    if mode == "replace" else []
                ),
            })
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def bindings_upload(self) -> dict[str, Any]:
        try:
            lm = self._require_lm()
            body = await _body()
            mode = str(body.get("mode") or "merge").strip().lower()
            if mode not in {"merge", "replace"}:
                return _err("mode 必须是 merge 或 replace")
            operator = str(body.get("operator") or "").strip()
            reviewer = str(body.get("reviewer") or "").strip()
            reason = str(body.get("reason") or "").strip()
            if not operator:
                return _err("请填写操作人")
            if not reason:
                return _err("请填写上传依据")
            content = str(body.get("content") or "")
            rows, errors = _parse_upload(content, str(body.get("filename") or ""))
            if not rows:
                return _err("没有可用的绑定行" + (f"：{errors[0]}" if errors else ""))
            if _duplicate_accounts(rows):
                return _err("文件内账号重复，请合并后重新上传")
            active = None
            if mode == "replace":
                if errors:
                    return _err("replace 含错误行，未执行任何变更：" + errors[0])
                # replace 必须先预览：用同一份行 + 当前绑定快照校验确认令牌。
                if not _boolean(body, "preview_confirmed"):
                    return _err("replace 模式需要先预览并确认")
                active = await lm.list_binding_records(include_history=False)
                expected = _preview_token(rows, mode, active, _content_hash(content))
                if str(body.get("preview_token") or "") != expected:
                    return _err("预览已过期或文件已变化，请重新预览并确认")
            result = await lm.upload_bindings(
                rows, mode=mode, file_hash=_content_hash(content),
                operator=operator, reviewer=reviewer, reason=reason,
                request_id=uuid4().hex, expected_active=active,
            )
            result["errors"] = errors
            result["mode"] = mode
            logger.info(f"[congling] 绑定文件上传 mode={mode} rows={len(rows)} operator={operator}")
            return _ok(result)
        except Exception as exc:
            return _err(repr(exc)[:200])
