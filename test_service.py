"""验证 HTTP 服务：基础契约、领域接口、幂等重放与错误映射。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from domain import EventStore, HeritageService
from service import (SERVICE_ID, health_payload, load_contract,
                     make_handler)

TECHNIQUES = [{"名称": "操影启蒙", "阶段": "启蒙", "前置技法": []}]


class HttpTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = HeritageService(EventStore())
        cls.service.seed_skill_catalog(TECHNIQUES)
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(cls.service))
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
        req = Request(f"{self.base_url}{path}", data=data, method=method,
                      headers={"Content-Type": "application/json",
                               **(headers or {})})
        with urlopen(req, timeout=2) as response:
            return response.status, json.load(response)

    def read_json(self, path):
        with urlopen(f"{self.base_url}{path}", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(),
                             "application/json")
            return json.load(response)

    def request_error(self, method, path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
        req = Request(f"{self.base_url}{path}", data=data, method=method,
                      headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(req, timeout=2)
        payload = json.load(error.exception)
        return error.exception.code, payload


class ServiceContractTest(HttpTestBase):
    def test_health_identity(self):
        self.assertEqual(self.read_json("/health"), health_payload())

    def test_contract_identity_and_rules(self):
        contract = self.read_json("/contract")
        self.assertEqual(contract, load_contract())
        self.assertEqual(contract["service_id"], SERVICE_ID)
        self.assertGreaterEqual(len(contract["invariants"]), 3)
        self.assertTrue(
            any("珍贵原件借用需要两名不同责任人分别批准" in rule
                for rule in contract["invariants"]))

    def test_unknown_route_is_hidden(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()


class ApiFlowTest(HttpTestBase):
    def test_full_api_flow(self):
        status, person = self.request("POST", "/people", {
            "人员": "per_tan", "姓名": "谭师傅", "角色": ["传承人"]})
        self.assertEqual(status, 201)
        status, created = self.request("POST", "/objects", {
            "类型": "皮影", "名称": "清代影偶", "珍贵原件": True,
            "物件": "obj_a"})
        self.assertEqual(status, 201)
        self.assertEqual(created["事件"]["payload"]["物件"], "obj_a")

        # 珍贵原件无双人批准 -> 400
        code, payload = self.request_error("POST", "/objects/obj_a/borrow", {
            "用途": "教学", "领用人": "per_tan"})
        self.assertEqual(code, 400)
        self.assertIn("两名责任人", payload["错误"])

        # 两次批准后可借用
        self.request("POST", "/objects/obj_a/approvals", {"批准人": "per_tan"})
        self.request("POST", "/people", {
            "人员": "per_li", "姓名": "李师傅", "角色": ["传承人"]})
        self.request("POST", "/objects/obj_a/approvals", {"批准人": "per_li"})
        _, borrowed = self.request("POST", "/objects/obj_a/borrow", {
            "用途": "教学", "领用人": "per_tan",
            "批准人": ["per_tan", "per_li"]})
        self.assertFalse(borrowed["重复"])

        view = self.read_json("/objects/obj_a")
        self.assertEqual(view["当前状态"], "教学借用")

    def test_idempotency_key_header_dedupes(self):
        self.request("POST", "/objects", {
            "类型": "道具", "名称": "幕布", "物件": "obj_d"})
        headers = {"Idempotency-Key": "device-0001"}
        status1, first = self.request(
            "POST", "/objects/obj_d/inspections",
            {"温度": 20, "湿度": 50, "结论": "正常", "巡检人": "per_tan"},
            headers=headers)
        status2, second = self.request(
            "POST", "/objects/obj_d/inspections",
            {"温度": 99, "湿度": 99, "结论": "异常", "巡检人": "per_tan"},
            headers=headers)
        self.assertEqual(status1, 201)
        self.assertEqual(status2, 200)
        self.assertEqual(first["事件"]["id"], second["事件"]["id"])
        self.assertTrue(second["重复"])

        timeline = self.read_json("/objects/obj_d/timeline")
        self.assertEqual(
            sum(1 for e in timeline["时间线"] if e["事件"] == "环境巡检"),
            1)

    def test_body_idempotency_key_dedupes(self):
        self.request("POST", "/objects", {
            "类型": "乐器", "名称": "堂鼓", "物件": "obj_drum"})
        body = {"用途": "教学", "领用人": "per_tan", "幂等键": "scan-9"}
        _, first = self.request("POST", "/objects/obj_drum/borrow", body)
        _, second = self.request("POST", "/objects/obj_drum/borrow", body)
        self.assertFalse(first["重复"])
        self.assertTrue(second["重复"])

    def test_unknown_entity_is_404_on_get(self):
        code, payload = self.request_error("GET", "/objects/obj_missing")
        self.assertEqual(code, 404)
        self.assertIn("物件不存在", payload["错误"])

    def test_bad_json_is_400(self):
        req = Request(f"{self.base_url}/objects", data=b'{"bad":',
                      method="POST",
                      headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(req, timeout=2)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
