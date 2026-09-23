"""皮影传习与藏品养护的 HTTP 服务。

在基础健康检查与领域契约之外，提供藏品全生命周期、学徒成长、剧目版本
等 JSON 接口。命令接口支持 ``Idempotency-Key`` 请求头或请求体中的
``幂等键``：扫码设备断网恢复后重放同一请求只记一次账。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from domain import DomainError, EventStore, HeritageService

SERVICE_ID = "shadow-puppet-heritage"
SERVICE_NAME = "皮影传习与藏品养护"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID:
        raise ValueError("领域契约与服务身份不一致")
    return contract


def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def make_handler(service):
    """按给定领域服务构造 HTTP 处理器（便于测试注入内存存储）。"""

    class Handler(BaseHTTPRequestHandler):
        """藏品、教学、剧目的读写接口。"""

        def do_GET(self):
            try:
                self._route_get()
            except DomainError as exc:
                self._send_json({"错误": str(exc)}, 404)

        def do_POST(self):
            try:
                self._route_post()
            except DomainError as exc:
                self._send_json({"错误": str(exc)}, 400)
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                self._send_json({"错误": f"请求体不合法：{exc}"}, 400)

        # ---- GET 路由 -----------------------------------------------------

        def _route_get(self):
            path = self.path.rstrip("/") or "/"
            if path == "/health":
                self._send_json(health_payload()); return
            if path == "/contract":
                self._send_json(load_contract()); return
            if path == "/catalog":
                self._send_json({"技法": service.catalog()}); return
            if path == "/people":
                self._send_json({"人员": service.list_people()}); return
            if path == "/objects":
                self._send_json({"物件": service.list_objects()}); return
            if path == "/students":
                self._send_json({"学徒": service.list_students()}); return
            if path == "/plays":
                self._send_json({"剧目": service.list_plays()}); return

            parts = [p for p in path.split("/") if p]
            if len(parts) == 3 and parts[0] == "objects":
                self._send_object(parts[1], parts[2]); return
            if len(parts) == 2:
                if parts[0] == "objects":
                    self._send_json(service.object_view(parts[1])); return
                if parts[0] == "students":
                    self._send_json(service.student_view(parts[1])); return
                if parts[0] == "plays":
                    self._send_json(service.play_view(parts[1])); return
            self.send_error(404)

        def _send_object(self, object_id, sub):
            if sub == "timeline":
                self._send_json({"时间线": service.object_timeline(object_id)})
                return
            self.send_error(404)

        # ---- POST 路由 ----------------------------------------------------

        def _route_post(self):
            body = self._read_body()
            idem = body.pop("幂等键", None) or self.headers.get("Idempotency-Key")
            path = self.path.rstrip("/")
            parts = [p for p in path.split("/") if p]
            target = self._target(parts)
            if target is None:
                self.send_error(404); return
            event, created = target(body, idem)
            self._send_json({"事件": event, "重复": not created},
                            200 if not created else 201)

        def _target(self, parts):
            """返回 (body, idem) -> (event, created) 的命令闭包。"""
            s = service

            if parts == ["catalog"]:
                return lambda b, i: s.seed_skill_catalog(b["技法"], idem=i)
            if parts == ["people"]:
                return lambda b, i: s.register_person(
                    b["人员"], b["姓名"], b["角色"], idem=i)
            if parts == ["objects"]:
                return lambda b, i: s.register_object(
                    b["类型"], b["名称"],
                    precious=b.get("珍贵原件", False),
                    location=b.get("存放位置", "库房"),
                    acquired=b.get("入藏日期"),
                    object_id=b.get("物件"),
                    登记人=b.get("登记人", ""), idem=i)
            if parts == ["students"]:
                return lambda b, i: s.register_student(
                    b["姓名"], b["出生日期"],
                    student_id=b.get("学徒"),
                    mentor=b.get("启蒙教师", ""),
                    note=b.get("备注", ""), idem=i)
            if parts == ["plays"]:
                return lambda b, i: s.register_play(
                    b["剧名"], play_id=b.get("剧目"),
                    traditional=b.get("传统剧目", True),
                    note=b.get("备注", ""), idem=i)

            if len(parts) >= 2 and parts[0] == "objects":
                oid, tail = parts[1], parts[2:]
                if tail == ["replicas"]:
                    return lambda b, i: s.add_replica(
                        oid, b["名称"], replica_id=b.get("复制品"),
                        maker=b.get("制作人", ""),
                        note=b.get("备注", ""), idem=i)
                if tail == ["inspections"]:
                    return lambda b, i: s.record_environment(
                        oid, b["温度"], b["湿度"], b["结论"],
                        b["巡检人"], idem=i,
                        photo=b.get("病害照片", ""),
                        note=b.get("备注", ""))
                if tail == ["disease-photos"]:
                    return lambda b, i: s.attach_disease_photo(
                        oid, b["照片"], b["病害描述"], b["记录人"], idem=i)
                if tail == ["repairs", "start"]:
                    return lambda b, i: s.start_repair(
                        oid, b["方案"], b["拟用材料"], b["修复人"],
                        approver=b.get("负责人", ""), idem=i)
                if tail == ["repairs", "finish"]:
                    return lambda b, i: s.finish_repair(
                        oid, b["实际材料"], b["修复人"],
                        after_state=b.get("后状态", "在库"),
                        note=b.get("备注", ""), idem=i)
                if tail == ["approvals"]:
                    return lambda b, i: s.approve_use(
                        oid, b["批准人"], idem=i, note=b.get("备注", ""))
                if tail == ["borrow"]:
                    return lambda b, i: s.borrow_object(
                        oid, b["用途"], b["领用人"],
                        approvals=b.get("批准人", []), idem=i,
                        play_id=b.get("关联剧目", ""),
                        due=b.get("预计归还", ""),
                        note=b.get("备注", ""))
                if tail == ["return"]:
                    return lambda b, i: s.return_object(
                        oid, b["归还人"],
                        condition=b.get("归还状态", "完好"),
                        idem=i, note=b.get("备注", ""))

            if len(parts) >= 2 and parts[0] == "students":
                sid, tail = parts[1], parts[2:]
                if tail == ["mentor"]:
                    return lambda b, i: s.change_mentor(
                        sid, b["新师承"], b["原因"], idem=i)
                if tail == ["attendance"]:
                    return lambda b, i: s.mark_attendance(
                        sid, b["状态"], b["课程"], b["教师"], idem=i,
                        note=b.get("备注", ""))
                if tail == ["reviews"]:
                    return lambda b, i: s.review_work(
                        sid, b["技法"], b["结论"], b["复核人"],
                        work_ref=b.get("作品", ""), idem=i,
                        note=b.get("备注", ""))
                if tail == ["practice"]:
                    return lambda b, i: s.practice(
                        sid, b["技法"], b["对象"], b["指导教师"],
                        replica=b.get("复制品", True), idem=i,
                        note=b.get("备注", ""))

            if len(parts) >= 2 and parts[0] == "plays":
                pid, tail = parts[1], parts[2:]
                if tail == ["versions"]:
                    return lambda b, i: s.add_play_version(
                        pid, b["版本号"], b["创作者"], b["整理人"],
                        b["内容提要"], based_on=b.get("依据版本", ""),
                        idem=i)
                if tail == ["permissions"]:
                    return lambda b, i: s.grant_permission(
                        pid, b["创作者"], b["范围"],
                        valid_from=b.get("生效日", ""),
                        valid_to=b.get("到期日", ""),
                        note=b.get("备注", ""), idem=i)
                if tail == ["performances"]:
                    return lambda b, i: s.register_performance(
                        pid, b["版本"], b["场地"], b["演出负责人"],
                        objects=b.get("使用物件", []),
                        date_str=b.get("日期", ""),
                        note=b.get("备注", ""), idem=i)
            return None

        # ---- 收发帮助 -----------------------------------------------------

        def _read_body(self):
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))

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


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--data", default=str(Path(__file__).with_name("data.json")),
                        help="事件存储文件（JSON，原子写入）")
    args = parser.parse_args()
    if args.check:
        contract = load_contract()
        assert contract["states"] and contract["invariants"]
        print("基础检查通过")
        return
    service = HeritageService(EventStore(args.data))
    handler = make_handler(service)
    ThreadingHTTPServer(("0.0.0.0", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
