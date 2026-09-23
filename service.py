"""皮影传习与藏品养护的服务入口。

在健康检查与领域契约之外，提供藏品养护、借用批准、学徒成长、剧目版本与
公开演出的 HTTP 接口；/sync 供断网扫码设备恢复后幂等批量补传。
数据以 JSON 快照保存（--data 指定路径，默认仅内存）。
"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from domain import (
    HeritageSystem, DomainError, NotFoundError, ConflictError,
    load_contract as load_domain_contract,
)

SERVICE_ID = "shadow-puppet-heritage"
SERVICE_NAME = "皮影传习与藏品养护"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID:
        raise ValueError("领域契约与服务身份不一致")
    return contract


# 启动时一并校验领域层契约可被正确加载
load_domain_contract()


def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


# 简单 POST 路由：路径正则 -> (领域方法名, 从路径提取的参数名)
POST_ROUTES = [
    (re.compile(r"^/persons$"), "register_person", ()),
    (re.compile(r"^/artifacts$"), "register_artifact", ()),
    (re.compile(r"^/inspections$"), "add_inspection", ()),
    (re.compile(r"^/artifacts/(?P<artifact_code>[^/]+)/diseases$"), "report_disease", ("artifact_code",)),
    (re.compile(r"^/artifacts/(?P<artifact_code>[^/]+)/conservations$"), "conserve", ("artifact_code",)),
    (re.compile(r"^/artifacts/(?P<artifact_code>[^/]+)/repairs/start$"), "start_repair", ("artifact_code",)),
    (re.compile(r"^/artifacts/(?P<artifact_code>[^/]+)/repairs/finish$"), "finish_repair", ("artifact_code",)),
    (re.compile(r"^/loans$"), "create_loan", ()),
    (re.compile(r"^/loans/(?P<loan_id>[^/]+)/return$"), "return_loan", ("loan_id",)),
    (re.compile(r"^/apprentices$"), "register_apprentice", ()),
    (re.compile(r"^/apprentices/(?P<apprentice_id>[^/]+)/certifications$"), "certify_skill", ("apprentice_id",)),
    (re.compile(r"^/apprentices/(?P<apprentice_id>[^/]+)/absences$"), "record_absence", ("apprentice_id",)),
    (re.compile(r"^/apprentices/(?P<apprentice_id>[^/]+)/mentor$"), "change_mentor", ("apprentice_id",)),
    (re.compile(r"^/apprentices/(?P<apprentice_id>[^/]+)/reviews$"), "review_work", ("apprentice_id",)),
    (re.compile(r"^/lessons$"), "record_lesson", ()),
    (re.compile(r"^/plays$"), "register_play", ()),
    (re.compile(r"^/plays/(?P<play_id>[^/]+)/versions$"), "add_script_version", ("play_id",)),
    (re.compile(r"^/performances$"), "register_performance", ()),
]


def make_handler(system, data_path=None):
    """构造绑定指定领域系统（与可选快照路径）的请求处理器。"""

    def persist():
        if data_path is not None:
            system.save(data_path)

    class Handler(BaseHTTPRequestHandler):
        """提供健康检查、契约读取与业务接口。"""

        def do_GET(self):
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if path == "/health":
                self._send_json(health_payload()); return
            if path == "/contract":
                self._send_json(load_contract()); return
            match = re.fullmatch(r"/artifacts/(?P<code>[^/]+)", path)
            if match:
                self._call(lambda: system.artifact_detail(match["code"])); return
            match = re.fullmatch(r"/apprentices/(?P<apprentice_id>[^/]+)", path)
            if match:
                self._call(lambda: system.apprentice_detail(match["apprentice_id"])); return
            match = re.fullmatch(r"/apprentices/(?P<apprentice_id>[^/]+)/can-operate", path)
            if match:
                self._can_operate(match["apprentice_id"], query); return
            self.send_error(404)

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/sync":
                self._sync(); return
            body = self._read_body()
            if body is None:
                return
            for pattern, method, path_args in POST_ROUTES:
                match = pattern.fullmatch(path)
                if match:
                    kwargs = {name: match.group(name) for name in path_args}
                    kwargs.update(body)
                    def action():
                        result = getattr(system, method)(**kwargs)
                        if result.get("ok", False):
                            persist()
                        return result
                    self._call(action, status=201)
                    return
            self.send_error(404)

        def _can_operate(self, apprentice_id, query):
            try:
                artifact_code = query.get("artifact_code", [None])[0]
                skill = query.get("skill", [None])[0]
                on = query.get("date", [None])[0]
                if not artifact_code or not skill:
                    raise DomainError("query 参数 artifact_code 与 skill 必填")
                self._send_json(system.can_operate(apprentice_id, artifact_code, skill, on=on))
            except DomainError as error:
                self._send_error(error)

        def _sync(self):
            body = self._read_body()
            if body is None:
                return
            events = body.get("events")
            if not isinstance(events, list):
                self._send_json({"error": "请求体须为 {\"events\": [...]}"}, status=422)
                return
            def action():
                result = system.sync(events)
                persist()
                return result
            # 批量同步整体返回 200；逐条成败在 results 内体现
            self._call(action, status=200)

        def _call(self, action, status=200):
            try:
                self._send_json(action(), status=status)
            except DomainError as error:
                self._send_error(error)

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError
                return payload
            except (ValueError, json.JSONDecodeError):
                self._send_json({"error": "请求体必须是 JSON 对象"}, status=400)
                return None

        def _send_error(self, error):
            status = 404 if isinstance(error, NotFoundError) else (
                409 if isinstance(error, ConflictError) else 422)
            self._send_json({"error": str(error), "code": error.code}, status=status)

        def _send_json(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return Handler


# 默认内存系统（未指定 --data 时使用）
SYSTEM = HeritageSystem()
Handler = make_handler(SYSTEM)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--data", default=None, help="JSON 快照保存路径；不指定则仅内存")
    args = parser.parse_args()
    if args.check:
        contract = load_contract()
        assert contract["states"] and contract["invariants"]
        assert contract["entities"] and contract["state_transitions"]
        print("基础检查通过")
        return
    data_path = Path(args.data) if args.data else None
    system = HeritageSystem.load(data_path) if data_path else HeritageSystem()
    handler = make_handler(system, data_path)
    ThreadingHTTPServer(("0.0.0.0", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
