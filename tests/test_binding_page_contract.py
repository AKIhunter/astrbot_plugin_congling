"""绑定页面输入契约：真实解析器和 handler，合成请求，不加载模型或联网。"""

import importlib.util
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


def load_bindings():
    request = types.SimpleNamespace(json=AsyncMock(return_value={}))
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("binding-contract-test")
    web = types.ModuleType("astrbot.api.web")
    web.request = request
    path = Path(__file__).resolve().parents[1] / "page_api/bindings.py"
    spec = importlib.util.spec_from_file_location("_binding_contract_test", path)
    module = importlib.util.module_from_spec(spec)
    # 测试导入结束即恢复模块表，避免影响同仓库其他测试。
    with patch.dict(sys.modules, {"astrbot": types.ModuleType("astrbot"),
                                 "astrbot.api": api, "astrbot.api.web": web}):
        spec.loader.exec_module(module)
    return module, request


class BindingPageContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module, self.request = load_bindings()
        self.lm = types.SimpleNamespace(
            list_binding_records=AsyncMock(return_value=[
                {"person_id": "original", "platform": "qq", "account_id": "keep"}]),
            upload_bindings=AsyncMock(),
            review_binding_candidate=AsyncMock(return_value={"candidate": {"status": "approved"}}),
        )
        self.page = self.module.BindingMixin()
        self.page._require_lm = lambda: self.lm

    async def test_duplicate_json_keys_rejected_before_replace_can_write(self):
        samples = [
            '[{"person_id":"p1","platform":"qq","account_id":"keep","account_id":"other"}]',
            '[{"person_id":"p1","person_id":"p2","platform":"qq","account_id":"keep"}]',
            '{"rows":[{"person_id":"p1","platform":"qq","account_id":"keep"}],"rows":[]}',
            '[{"person_id":"p1","platform":"qq","account_id":"keep","account_\\u0069d":"other"}]',
            '[{"person_id":"p1","platform":"qq","account_id":"keep","extra":{"key":1,"key":2}}]',
            '[{"person_id":"p1","platform":"qq","account_id":"valid"},'
            '{"person_id":"p1","platform":"qq","account_id":"keep","account_id":"other"}]',
        ]
        for content in samples:
            with self.subTest(content=content):
                rows, errors = self.module._parse_upload(content, "synthetic.json")
                self.assertEqual(rows, [])
                self.assertTrue(any("重复键" in error for error in errors))
                self.request.json.return_value = {
                    "content": content, "filename": "synthetic.json", "mode": "replace",
                    "operator": "human", "reason": "synthetic", "preview_confirmed": True,
                    "preview_token": "old-token",
                }
                for handler in (self.page.bindings_upload_preview, self.page.bindings_upload):
                    response = await handler()
                    self.assertFalse(response["ok"])
                    self.assertIn("重复键", response["error"])
        self.lm.upload_bindings.assert_not_awaited()

    async def test_review_forwards_displayed_snapshot_without_refreshing_it(self):
        shown = {"candidate_id": "id", "person_id": "displayed", "note": "shown-note",
                 "updated_at": 1000.0}
        self.request.json.return_value = {
            "candidate_id": "id", "expected_candidate": shown, "approve": True,
            "reviewer": "human", "reason": "synthetic",
        }
        self.assertTrue((await self.page.bindings_candidates_review())["ok"])
        self.assertEqual(self.lm.review_binding_candidate.await_args.kwargs["expected_candidate"], shown)
        self.lm.list_binding_records.assert_not_awaited()

    def test_valid_json_and_csv_still_parse(self):
        for content, filename in [
            ('[{"person_id":"p1","platform":"qq","account_id":"keep"}]', "synthetic.json"),
            ("person_id,platform,account_id\np1,qq,keep\n", "synthetic.csv"),
        ]:
            rows, errors = self.module._parse_upload(content, filename)
            self.assertEqual(errors, [])
            self.assertEqual(rows[0]["account_id"], "keep")


if __name__ == "__main__":
    unittest.main()
