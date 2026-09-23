"""验证基础服务、领域契约与 HTTP 业务接口保持一致。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from domain import HeritageSystem
from service import Handler, SERVICE_ID, health_payload, load_contract, make_handler


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.system = HeritageSystem()
        cls.handler_cls = make_handler(cls.system)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.handler_cls)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)

    def request(self, path, payload=None, method=None):
        if payload is None:
            with urlopen(f"{self.base_url}{path}", timeout=2) as response:
                self.assertEqual(response.headers.get_content_type(), "application/json")
                return response.status, json.load(response)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(f"{self.base_url}{path}", data=body, method=method or "POST",
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=2) as response:
            return response.status, json.load(response)

    def request_error(self, path, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(f"{self.base_url}{path}", data=body,
                          headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        body = json.loads(error.exception.read())
        code = error.exception.code
        error.exception.close()
        return code, body

    def read_json(self, path):
        with urlopen(f"{self.base_url}{path}", timeout=2) as response:
            self.assertEqual(response.status, 200)
            return json.load(response)


class ServiceContractTest(HttpTest):
    def test_health_identity(self):
        self.assertEqual(self.read_json("/health"), health_payload())

    def test_contract_identity_and_rules(self):
        contract = self.read_json("/contract")
        self.assertEqual(contract, load_contract())
        self.assertEqual(contract["service_id"], SERVICE_ID)
        self.assertGreaterEqual(len(contract["invariants"]), 3)
        self.assertEqual(contract["contract_version"], "2.0")
        self.assertIn("artifact_tiers", contract)

    def test_unknown_route_is_hidden(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()


class BusinessHttpTest(HttpTest):
    def test_01_full_flow_over_http(self):
        status, person = self.request("/persons", {
            "person_id": "g1", "name": "李管理", "role": "藏品管理员"})
        self.assertEqual(status, 201)
        self.request("/persons", {
            "person_id": "c1", "name": "王传承", "role": "传承人", "skills": ["唱腔"]})
        status, artifact = self.request("/artifacts", {
            "code": "P001", "name": "老皮影", "category": "皮影", "tier": "珍贵原件"})
        self.assertEqual(status, 201)
        self.assertEqual(artifact["artifact"]["state"], "在库")

    def test_02_single_approver_rejected(self):
        code, body = self.request_error("/loans", {
            "artifact_code": "P001", "purpose": "教学",
            "borrower_id": "g1", "approvers": ["g1"]})
        self.assertEqual(code, 422)
        self.assertEqual(body["code"], "rule_violation")

    def test_03_two_approvers_and_trace(self):
        status, loan = self.request("/loans", {
            "artifact_code": "P001", "purpose": "教学",
            "borrower_id": "g1", "approvers": ["g1", "c1"]})
        self.assertEqual(status, 201)
        detail = self.read_json("/artifacts/P001")
        self.assertEqual(detail["artifact"]["state"], "教学借用")
        self.assertEqual(detail["loans"][0]["approvers"], ["g1", "c1"])

    def test_04_sync_dedup_over_http(self):
        before = len(self.read_json("/artifacts/P001")["conservations"])
        # P001 借用中，养护会失败；先登记一件在库藏品做养护幂等验证
        self.request("/artifacts", {
            "code": "P002", "name": "旧剧本", "category": "剧本", "tier": "普通原件"})
        status, outcome = self.request("/sync", {"events": [
            {"type": "conserve", "artifact_code": "P002", "action": "通风除尘",
             "operator": "g1", "event_id": "NET-1"},
            {"type": "conserve", "artifact_code": "P002", "action": "通风除尘",
             "operator": "g1", "event_id": "NET-1"},
        ]})
        self.assertEqual(status, 200)
        self.assertEqual([r["ok"] for r in outcome["results"]], [True, True])
        self.assertTrue(outcome["results"][1]["duplicate"])
        after = self.read_json("/artifacts/P002")
        self.assertEqual(len(after["conservations"]), before + 1)

    def test_05_not_found_and_bad_body(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/artifacts/NOPE", timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()
        code, body = self.request_error_raw("/artifacts", "不是json")
        self.assertEqual(code, 400)

    def request_error_raw(self, path, raw):
        request = Request(f"{self.base_url}{path}", data=raw.encode("utf-8"),
                          headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        body = json.loads(error.exception.read())
        code = error.exception.code
        error.exception.close()
        return code, body


if __name__ == "__main__":
    unittest.main()
