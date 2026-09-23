"""皮影传习与藏品养护的领域核心。

以只追加的事件流保存全部业务事实：藏品身份、养护、借用、学徒成长、
剧目版本与演出。任何命令都不覆盖历史，因此缺课、转师、复核都会留下
连续记录。事件按“身份 + 事件类型 + 幂等键”去重，扫码设备断网恢复后
重试同一条操作不会产生第二次养护或借出。
"""

import json
import threading
import uuid
from datetime import date
from pathlib import Path

# ---- 固定词表（与 domain_contract.json 保持一致）---------------------------

OBJECT_TYPES = ("皮影", "乐器", "剧本", "道具")
OBJECT_STATES = ("在库", "养护中", "教学借用", "演出借用", "限制使用")
ENV_RESULTS = ("正常", "异常")
BORROW_PURPOSES = ("教学", "演出")
SKILL_LEVELS = {"启蒙": 1, "入门": 2, "熟练": 3, "精通": 4}
REVIEW_RESULTS = ("通过", "复核中", "不通过")
PERMISSION_SCOPES = ("教学", "公开演出", "全场景")


class DomainError(Exception):
    """业务规则被违反；HTTP 层映射为 4xx。"""


def _new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _today():
    return date.today().isoformat()


class EventStore:
    """只追加事件存储，带幂等索引与崩溃安全的 JSON 持久化。"""

    def __init__(self, path=None):
        self._path = Path(path) if path else None
        self._events = []
        self._idem = {}          # (流, 类型, 幂等键) -> 已落库事件
        self._lock = threading.RLock()
        if self._path and self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for event in raw.get("events", []):
                self._index(event)
                self._events.append(event)

    def _index(self, event):
        key = event.get("idem")
        if key:
            self._idem[(event["stream"], event["type"], key)] = event

    @property
    def events(self):
        with self._lock:
            return list(self._events)

    def events_for(self, stream):
        with self._lock:
            return [e for e in self._events if e["stream"] == stream]

    def peek(self, stream, event_type, idem):
        """按幂等键查找已落库事件；不存在返回 None。"""
        if idem is None:
            return None
        with self._lock:
            return self._idem.get((stream, event_type, idem))

    def find_idem(self, event_type, idem):
        """跨流按 (事件类型, 幂等键) 查找。

        用于服务端生成身份的创建命令：重放请求还没有目标流，
        只能全局检索首次事件。
        """
        if idem is None:
            return None
        with self._lock:
            for (_stream, etype, key), event in self._idem.items():
                if etype == event_type and key == idem:
                    return event
            return None

    def append(self, stream, event_type, payload, idem=None):
        """追加事件；相同幂等键直接返回首次事件，不重复记账。"""
        with self._lock:
            if idem is not None:
                existed = self._idem.get((stream, event_type, idem))
                if existed:
                    return existed, False
            event = {
                "id": _new_id("evt"),
                "stream": stream,
                "type": event_type,
                "payload": payload,
                "date": payload.get("日期") or _today(),
                "idem": idem,
            }
            self._events.append(event)
            if idem is not None:
                self._idem[(stream, event_type, idem)] = event
            self._flush()
            return event, True

    def _flush(self):
        if not self._path:
            return
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"events": self._events}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)


class HeritageService:
    """藏品养护与传习教学的领域服务（命令 + 只读视图）。"""

    def __init__(self, store=None, path=None):
        self.store = store or EventStore(path)

    # ---- 通用工具 ---------------------------------------------------------

    def _require(self, condition, message):
        if not condition:
            raise DomainError(message)

    def _dedup(self, stream, event_type, idem):
        """命令入口的幂等快查；命中即返回 (首次事件, False)，否则 None。

        必须在任何依赖当前状态的校验之前调用，否则断网重放时状态已推进，
        校验会先于去重失败。
        """
        hit = self.store.peek(stream, event_type, idem)
        return None if hit is None else (hit, False)

    def _replay(self, event_type, idem):
        """跨流幂等快查，用于生成新身份的创建命令。"""
        hit = self.store.find_idem(event_type, idem)
        return None if hit is None else (hit, False)

    def _first(self, stream, event_type):
        for event in self.store.events_for(stream):
            if event["type"] == event_type:
                return event
        return None

    def _by_id(self, prefix, entity_id):
        create_types = {"obj": "物件登记", "stu": "学徒建档",
                        "per": "教师建档", "play": "剧目立项"}
        stream = f"{prefix}:{entity_id}"
        return stream, self._first(stream, create_types[prefix])

    def _require_person(self, person_id):
        _, person = self._by_id("per", person_id)
        self._require(person is not None, f"人员未登记：{person_id}")
        return person

    def _object(self, object_id):
        stream, event = self._by_id("obj", object_id)
        self._require(event is not None, f"物件不存在：{object_id}")
        return stream, event

    def _student(self, student_id):
        stream, event = self._by_id("stu", student_id)
        self._require(event is not None, f"学徒不存在：{student_id}")
        return stream, event

    def _play(self, play_id):
        stream, event = self._by_id("play", play_id)
        self._require(event is not None, f"剧目不存在：{play_id}")
        return stream, event

    def _technique(self, name):
        event = self._first("catalog:technique", "技法目录")
        catalog = event["payload"]["技法"] if event else []
        for item in catalog:
            if item["名称"] == name:
                self._require(item["阶段"] in SKILL_LEVELS,
                              f"技法阶段非法：{item['阶段']}")
                return item
        raise DomainError(f"未知技法：{name}")

    # ---- 目录与人员 -------------------------------------------------------

    def seed_skill_catalog(self, techniques, idem=None):
        """登记技法阶段目录。目录已存在时原样返回，供初始化脚本重复执行。"""
        existed = self._first("catalog:technique", "技法目录")
        if existed is not None:
            return existed, False
        known = {t["名称"] for t in techniques}
        for tech in techniques:
            self._require(tech["阶段"] in SKILL_LEVELS,
                          f"技法阶段非法：{tech['阶段']}")
            for need in tech.get("前置技法", []):
                self._require(need in known, f"前置技法不存在：{need}")
        return self.store.append(
            "catalog:technique", "技法目录", {"技法": techniques}, idem=idem
        )

    def register_person(self, person_id, name, roles, idem=None):
        replay = self._dedup(f"per:{person_id}", "教师建档", idem)
        if replay:
            return replay
        self._require(isinstance(roles, list) and roles, "至少指定一个角色")
        _, existed = self._by_id("per", person_id)
        self._require(existed is None, f"人员已登记：{person_id}")
        payload = {"人员": person_id, "姓名": name, "角色": roles}
        return self.store.append(
            f"per:{person_id}", "教师建档", payload, idem=idem
        )

    # ---- 物件身份与全生命周期 --------------------------------------------

    def register_object(self, object_type, name, *, precious=False,
                        location="库房", acquired=None, object_id=None,
                        idem=None, **extra):
        client_id = object_id is not None
        object_id = object_id or _new_id("obj")
        replay = (self._dedup(f"obj:{object_id}", "物件登记", idem)
                  if client_id else self._replay("物件登记", idem))
        if replay:
            return replay
        self._require(object_type in OBJECT_TYPES, f"物件类型非法：{object_type}")
        _, existed = self._by_id("obj", object_id)
        self._require(existed is None, f"物件身份已存在：{object_id}")
        payload = {
            "物件": object_id, "类型": object_type, "名称": name,
            "珍贵原件": bool(precious), "入藏日期": acquired or _today(),
            "存放位置": location,
        }
        payload.update(extra)
        event, created = self.store.append(
            f"obj:{object_id}", "物件登记", payload, idem=idem
        )
        if created:
            self.store.append(
                f"obj:{object_id}", "状态变更",
                {"日期": payload["入藏日期"], "由": None, "到": "在库",
                 "原因": "入藏登记", "操作人": extra.get("登记人", "")},
            )
        return event, created

    def add_replica(self, original_id, name, *, replica_id=None, idem=None,
                    maker="", note=""):
        """为珍贵原件制作复制品，复制品有独立身份并与原件关联。"""
        client_id = replica_id is not None
        replica_id = replica_id or _new_id("obj")
        replay = (self._dedup(f"obj:{replica_id}", "物件登记", idem)
                  if client_id else self._replay("物件登记", idem))
        if replay:
            return replay
        stream, original = self._object(original_id)
        _, existed = self._by_id("obj", replica_id)
        self._require(existed is None, f"物件身份已存在：{replica_id}")
        payload = {
            "物件": replica_id, "类型": original["payload"]["类型"],
            "名称": name, "珍贵原件": False, "入藏日期": _today(),
            "存放位置": "教学道具柜", "复制品": True,
            "原件": original_id, "制作人": maker, "备注": note,
        }
        event, created = self.store.append(
            f"obj:{replica_id}", "物件登记", payload, idem=idem
        )
        if created:
            self.store.append(
                f"obj:{replica_id}", "状态变更",
                {"日期": _today(), "由": None, "到": "在库",
                 "原因": "复制品入库", "操作人": maker},
            )
        return event, created

    def record_environment(self, object_id, temp, humidity, result,
                           inspector, *, idem=None, photo="", note=""):
        stream, _ = self._object(object_id)
        replay = self._dedup(stream, "环境巡检", idem)
        if replay:
            return replay
        self._require(result in ENV_RESULTS, f"巡检结论非法：{result}")
        payload = {"日期": _today(), "温度": temp, "湿度": humidity,
                   "结论": result, "巡检人": inspector,
                   "病害照片": photo, "备注": note}
        return self.store.append(stream, "环境巡检", payload, idem=idem)

    def start_repair(self, object_id, plan, materials, operator,
                     *, approver="", idem=None):
        stream, obj = self._object(object_id)
        replay = self._dedup(stream, "修复开始", idem)
        if replay:
            return replay
        state = self.object_state(object_id)
        self._require(state in ("在库", "限制使用"),
                      f"当前状态为{state}，不能开始修复")
        payload = {"日期": _today(), "方案": plan, "拟用材料": materials,
                   "修复人": operator, "负责人": approver,
                   "前状态": state}
        event, created = self.store.append(
            stream, "修复开始", payload, idem=idem)
        if created:
            self._transition(stream, state, "养护中", f"进入修复：{plan}",
                             operator)
        return event, created

    def finish_repair(self, object_id, materials_used, operator,
                      *, after_state="在库", idem=None, note=""):
        stream, _ = self._object(object_id)
        replay = self._dedup(stream, "修复完成", idem)
        if replay:
            return replay
        start = self._last_event(stream, "修复开始")
        self._require(start is not None, "该物件没有进行中的修复记录")
        finished = any(e["type"] == "修复完成"
                       and e["payload"]["修复开始"] == start["id"]
                       for e in self.store.events_for(stream))
        self._require(not finished, "该修复已完结，不能重复登记")
        payload = {"日期": _today(), "修复开始": start["id"],
                   "实际材料": materials_used, "修复人": operator,
                   "后状态": after_state, "备注": note}
        event, created = self.store.append(
            stream, "修复完成", payload, idem=idem)
        if created:
            self._transition(stream, "养护中", after_state,
                             "修复完成", operator)
        return event, created

    def attach_disease_photo(self, object_id, photo, description, recorder,
                             *, idem=None):
        stream, _ = self._object(object_id)
        replay = self._dedup(stream, "病害记录", idem)
        if replay:
            return replay
        payload = {"日期": _today(), "照片": photo,
                   "病害描述": description, "记录人": recorder}
        return self.store.append(stream, "病害记录", payload, idem=idem)

    # ---- 领用归还（原件双人批准）-----------------------------------------

    def approve_use(self, object_id, approver, *, idem=None, note=""):
        """责任人对原件外借作出一次批准；一件原件的一次外借需两人。"""
        stream, _ = self._object(object_id)
        replay = self._dedup(stream, "使用批准", idem)
        if replay:
            return replay
        self._require_person(approver)
        payload = {"日期": _today(), "批准人": approver, "备注": note}
        return self.store.append(stream, "使用批准", payload, idem=idem)

    def borrow_object(self, object_id, purpose, borrower, *,
                      approvals=(), idem=None, play_id="", due=None, note=""):
        stream, obj = self._object(object_id)
        replay = self._dedup(stream, "借出", idem)
        if replay:
            return replay
        self._require(purpose in BORROW_PURPOSES, f"借用用途非法：{purpose}")
        state = self.object_state(object_id)
        self._require(state == "在库", f"物件当前为{state}，不可领用")
        approvers = list(dict.fromkeys(approvals))  # 去重保序
        if obj["payload"].get("珍贵原件"):
            self._require(len(approvers) >= 2,
                          "珍贵原件进入课堂或演出须经两名责任人批准")
            self._require(len(approvers) == len(set(approvers)),
                          "两名批准人不得为同一人")
            for approver in approvers:
                self._require_person(approver)
            recorded = {e["payload"]["批准人"]
                        for e in self.store.events_for(stream)
                        if e["type"] == "使用批准"}
            missing = [a for a in approvers if a not in recorded]
            self._require(not missing,
                          f"批准缺少正式批准记录：{'、'.join(missing)}")
        target = "教学借用" if purpose == "教学" else "演出借用"
        payload = {"日期": _today(), "用途": purpose, "领用人": borrower,
                   "批准人": approvers, "关联剧目": play_id,
                   "预计归还": due or "", "备注": note}
        event, created = self.store.append(stream, "借出", payload, idem=idem)
        if created:
            self._transition(stream, "在库", target,
                             f"{purpose}领用", borrower)
        return event, created

    def return_object(self, object_id, returner, *, condition="完好",
                      idem=None, note=""):
        stream, _ = self._object(object_id)
        replay = self._dedup(stream, "归还", idem)
        if replay:
            return replay
        state = self.object_state(object_id)
        self._require(state in ("教学借用", "演出借用"),
                      f"物件当前为{state}，没有未归还的借出")
        payload = {"日期": _today(), "归还人": returner,
                   "归还状态": condition, "备注": note}
        event, created = self.store.append(stream, "归还", payload, idem=idem)
        if created:
            target = "限制使用" if condition in ("损坏", "病害加重") else "在库"
            self._transition(stream, state, target,
                             f"归还：{condition}", returner)
        return event, created

    # ---- 学徒成长（历程永不抹去）-----------------------------------------

    def register_student(self, name, birth_date, *, student_id=None,
                         mentor="", idem=None, note=""):
        client_id = student_id is not None
        student_id = student_id or _new_id("stu")
        replay = (self._dedup(f"stu:{student_id}", "学徒建档", idem)
                  if client_id else self._replay("学徒建档", idem))
        if replay:
            return replay
        _, existed = self._by_id("stu", student_id)
        self._require(existed is None, f"学徒身份已存在：{student_id}")
        payload = {"学徒": student_id, "姓名": name, "出生日期": birth_date,
                   "入门日期": _today(), "启蒙教师": mentor, "备注": note}
        event, created = self.store.append(
            f"stu:{student_id}", "学徒建档", payload, idem=idem)
        if created and mentor:
            self.store.append(
                f"stu:{student_id}", "师承变更",
                {"日期": payload["入门日期"], "自": "", "至": mentor,
                 "原因": "入门师承"})
        return event, created

    def change_mentor(self, student_id, new_mentor, reason, *, idem=None):
        stream, student = self._student(student_id)
        replay = self._dedup(stream, "师承变更", idem)
        if replay:
            return replay
        current = self.student_view(student_id)["当前师承"]
        self._require(new_mentor != current, "新师承与当前师承相同")
        payload = {"日期": _today(), "自": current, "至": new_mentor,
                   "原因": reason}
        return self.store.append(stream, "师承变更", payload, idem=idem)

    def mark_attendance(self, student_id, status, course, teacher,
                        *, idem=None, note=""):
        stream, _ = self._student(student_id)
        replay = self._dedup(stream, "考勤", idem)
        if replay:
            return replay
        self._require(status in ("到课", "缺课", "请假", "补课"),
                      f"考勤状态非法：{status}")
        payload = {"日期": _today(), "状态": status, "课程": course,
                   "教师": teacher, "备注": note}
        return self.store.append(stream, "考勤", payload, idem=idem)

    def review_work(self, student_id, technique, result, reviewer,
                    *, work_ref="", idem=None, note=""):
        stream, _ = self._student(student_id)
        replay = self._dedup(stream, "作品复核", idem)
        if replay:
            return replay
        self._require_person(reviewer)
        tech = self._technique(technique)
        self._require(result in REVIEW_RESULTS, f"复核结论非法：{result}")
        if result == "通过":
            passed = self.student_view(student_id)["已通过技法"]
            self._require(technique not in passed,
                          f"技法「{technique}」已通过，无需重复登记")
            for need in tech.get("前置技法", []):
                self._require(need in passed,
                              f"须先通过前置技法：{need}")
        payload = {"日期": _today(), "技法": technique,
                   "阶段": tech["阶段"], "结论": result,
                   "复核人": reviewer, "作品": work_ref, "备注": note}
        event, created = self.store.append(
            stream, "作品复核", payload, idem=idem)
        if created and result == "通过":
            self.store.append(
                stream, "技法通过",
                {"日期": _today(), "技法": technique,
                 "阶段": tech["阶段"], "复核": event["id"]})
        return event, created

    def practice(self, student_id, technique, object_id, teacher,
                 *, replica=True, idem=None, note=""):
        """登记一次练习；学徒只能操作已通过技法允许的对象。"""
        stu_stream, _ = self._student(student_id)
        replay = self._dedup(stu_stream, "练习记录", idem)
        if replay:
            return replay
        obj_stream, obj = self._object(object_id)
        self._require_person(teacher)
        tech = self._technique(technique)
        view = self.student_view(student_id)
        self._require(technique in view["已通过技法"],
                      f"学徒尚未通过技法「{technique}」，不得操作")
        if not replica:
            self._require(not obj["payload"].get("珍贵原件", False),
                          "珍贵原件不得用于日常练习，须走领用批准")
            state = self.object_state(object_id)
            self._require(state == "在库",
                          f"物件当前为{state}，不能用于练习")
        else:
            self._require(obj["payload"].get("复制品", False)
                          or not obj["payload"].get("珍贵原件", False),
                          "标记为复制品练习，但该物件是珍贵原件")
        payload = {"日期": _today(), "技法": technique, "阶段": tech["阶段"],
                   "对象": object_id, "复制品": bool(replica),
                   "指导教师": teacher, "备注": note}
        event, created = self.store.append(
            stu_stream, "练习记录", payload, idem=idem)
        if created and not replica:
            self.store.append(
                obj_stream, "使用记录",
                {"日期": _today(), "学徒": student_id,
                 "技法": technique, "练习": event["id"]})
        return event, created

    # ---- 剧目版本与创作者许可 ---------------------------------------------

    def register_play(self, title, play_id=None, *, traditional=True,
                      idem=None, note=""):
        client_id = play_id is not None
        play_id = play_id or _new_id("play")
        replay = (self._dedup(f"play:{play_id}", "剧目立项", idem)
                  if client_id else self._replay("剧目立项", idem))
        if replay:
            return replay
        _, existed = self._by_id("play", play_id)
        self._require(existed is None, f"剧目身份已存在：{play_id}")
        payload = {"剧目": play_id, "剧名": title,
                   "传统剧目": bool(traditional), "备注": note}
        return self.store.append(
            f"play:{play_id}", "剧目立项", payload, idem=idem)

    def add_play_version(self, play_id, label, creators, editor, summary,
                         *, based_on="", idem=None):
        stream, _ = self._play(play_id)
        replay = self._dedup(stream, "剧目版本", idem)
        if replay:
            return replay
        versions = self.play_view(play_id)["版本"]
        self._require(not any(v["版本号"] == label for v in versions),
                      f"版本号已存在：{label}")
        if based_on:
            self._require(any(v["版本号"] == based_on for v in versions),
                          f"所依据版本不存在：{based_on}")
        self._require(creators, "至少登记一名创作者")
        payload = {"日期": _today(), "版本号": label, "依据版本": based_on,
                   "创作者": list(creators), "整理人": editor,
                   "内容提要": summary}
        return self.store.append(stream, "剧目版本", payload, idem=idem)

    def grant_permission(self, play_id, creator, scope, *,
                         valid_from="", valid_to="", idem=None, note=""):
        stream, _ = self._play(play_id)
        replay = self._dedup(stream, "许可授权", idem)
        if replay:
            return replay
        self._require(scope in PERMISSION_SCOPES, f"许可范围非法：{scope}")
        creators = self.play_view(play_id)["创作者"]
        self._require(creator in creators,
                      f"{creator} 不是该剧目前任何版本登记的创作者")
        payload = {"日期": _today(), "创作者": creator, "范围": scope,
                   "生效日": valid_from or _today(), "到期日": valid_to,
                   "备注": note}
        return self.store.append(stream, "许可授权", payload, idem=idem)

    def register_performance(self, play_id, version_label, venue, manager,
                             *, objects=(), idem=None, date_str="", note=""):
        stream, _ = self._play(play_id)
        replay = self._dedup(stream, "公开演出", idem)
        if replay:
            return replay
        play = self.play_view(play_id)
        version = next((v for v in play["版本"]
                        if v["版本号"] == version_label), None)
        self._require(version is not None, f"剧目版本不存在：{version_label}")
        day = date_str or _today()
        active = self._active_permissions(play_id, day)
        covered = {p["创作者"] for p in active
                   if p["范围"] in ("公开演出", "全场景")}
        missing = [c for c in version["创作者"] if c not in covered]
        self._require(not missing,
                      f"以下创作者未授予有效公开演出许可：{'、'.join(missing)}")
        for object_id in objects:
            state = self.object_state(object_id)
            self._require(state == "演出借用",
                          f"物件 {object_id} 当前为{state}，未办理演出借用")
        payload = {"日期": day, "剧目": play_id, "版本": version_label,
                   "场次版本": version["版本事件"],
                   "场地": venue, "演出负责人": manager,
                   "使用物件": list(objects), "备注": note}
        return self.store.append(stream, "公开演出", payload, idem=idem)

    # ---- 内部帮助 ---------------------------------------------------------

    def _last_event(self, stream, event_type):
        result = None
        for event in self.store.events_for(stream):
            if event["type"] == event_type:
                result = event
        return result

    def _transition(self, stream, src, dst, reason, operator):
        return self.store.append(
            stream, "状态变更",
            {"日期": _today(), "由": src, "到": dst,
             "原因": reason, "操作人": operator})

    def _active_permissions(self, play_id, day):
        stream, _ = self._play(play_id)
        result = []
        for event in self.store.events_for(stream):
            if event["type"] != "许可授权":
                continue
            p = event["payload"]
            if p["生效日"] <= day and (not p["到期日"] or p["到期日"] >= day):
                result.append(p)
        return result

    # ---- 只读视图 ---------------------------------------------------------

    def catalog(self):
        event = self._first("catalog:technique", "技法目录")
        return list(event["payload"]["技法"]) if event else []

    def list_people(self):
        people = []
        for event in self.store.events:
            if event["type"] == "教师建档":
                people.append(event["payload"])
        return people

    def list_objects(self):
        result = []
        for event in self.store.events:
            if event["type"] == "物件登记":
                p = dict(event["payload"])
                p["当前状态"] = self.object_state(p["物件"])
                result.append(p)
        return result

    def object_state(self, object_id):
        current = "在库"
        found = False
        for event in self.store.events_for(f"obj:{object_id}"):
            if event["type"] == "物件登记":
                found = True
            if event["type"] == "状态变更":
                current = event["payload"]["到"]
        if not found:
            raise DomainError(f"物件不存在：{object_id}")
        return current

    def object_timeline(self, object_id):
        """一件道具的连续记录：巡检、病害、修复、批准、借出归还、使用。"""
        stream, obj = self._object(object_id)
        detail_types = {"环境巡检", "病害记录", "修复开始", "修复完成",
                        "使用批准", "借出", "归还", "使用记录", "状态变更"}
        timeline = [
            {"日期": e["date"], "事件": e["type"], **e["payload"]}
            for e in self.store.events_for(stream)
            if e["type"] in detail_types
        ]
        timeline.sort(key=lambda e: (e["日期"], e.get("修复开始", "")))
        return timeline

    def object_view(self, object_id):
        """从一件道具看清状态、材料、责任与参演剧目。"""
        stream, obj = self._object(object_id)
        events = self.store.events_for(stream)
        materials, repairs = [], []
        for e in events:
            if e["type"] == "修复开始":
                repairs.append({"修复": e["id"], "开始": e["payload"],
                                "完成": None})
            elif e["type"] == "修复完成":
                materials.extend(e["payload"]["实际材料"])
                for item in repairs:
                    if item["修复"] == e["payload"]["修复开始"]:
                        item["完成"] = e["payload"]
        borrows, open_borrow = [], None
        for e in events:
            if e["type"] == "借出":
                open_borrow = {"借出": e["payload"], "归还": None}
                borrows.append(open_borrow)
            elif e["type"] == "归还" and open_borrow is not None:
                open_borrow["归还"] = e["payload"]
                open_borrow = None
        play_ids = {b["借出"].get("关联剧目") for b in borrows
                    if b["借出"].get("关联剧目")}
        used_in_performances = []
        for e in self.store.events:
            if e["type"] == "公开演出" and object_id in e["payload"]["使用物件"]:
                used_in_performances.append(
                    {"日期": e["date"], "剧目": e["payload"]["剧目"],
                     "版本": e["payload"]["版本"], "场地": e["payload"]["场地"]})
                play_ids.add(e["payload"]["剧目"])
        plays = [self.play_view(pid)["剧名"] for pid in sorted(play_ids)]
        inspections = [e["payload"] for e in events
                       if e["type"] in ("环境巡检", "病害记录")]
        return {
            "身份": obj["payload"],
            "当前状态": self.object_state(object_id),
            "巡检与病害": inspections,
            "修复记录": repairs,
            "修复材料": sorted(set(materials)),
            "借用记录": borrows,
            "参与剧目": plays,
            "演出记录": used_in_performances,
            "时间线": self.object_timeline(object_id),
        }

    def student_view(self, student_id):
        """从一名学徒追到成果、指导者与实际练习对象。"""
        stream, student = self._student(student_id)
        events = self.store.events_for(stream)
        passed, level = {}, 0
        attendance, reviews, practices, mentors = [], [], [], []
        current_mentor = ""
        for e in events:
            p = e["payload"]
            if e["type"] == "师承变更":
                mentors.append(p)
                current_mentor = p["至"]
            elif e["type"] == "技法通过":
                passed[p["技法"]] = p["阶段"]
                level = max(level, SKILL_LEVELS[p["阶段"]])
            elif e["type"] == "考勤":
                attendance.append(p)
            elif e["type"] == "作品复核":
                reviews.append(p)
            elif e["type"] == "练习记录":
                row = dict(p)
                row["对象名称"] = self._object_name(p["对象"])
                practices.append(row)
        level_name = next((n for n, lv in SKILL_LEVELS.items() if lv == level),
                          "")
        return {
            "身份": student["payload"],
            "当前师承": current_mentor,
            "师承历程": mentors,
            "已通过技法": sorted(passed),
            "技法阶段": passed,
            "阶段水平": level_name,
            "考勤": attendance,
            "作品复核": reviews,
            "练习记录": practices,
        }

    def _object_name(self, object_id):
        event = self._first(f"obj:{object_id}", "物件登记")
        return event["payload"]["名称"] if event else object_id

    def can_practice(self, student_id, technique):
        view = self.student_view(student_id)
        return technique in view["已通过技法"]

    def play_view(self, play_id):
        stream, head = self._play(play_id)
        versions, creators = [], set()
        permissions, performances = [], []
        for e in self.store.events_for(stream):
            if e["type"] == "剧目版本":
                creators.update(e["payload"]["创作者"])
                versions.append({"版本号": e["payload"]["版本号"],
                                 "依据版本": e["payload"]["依据版本"],
                                 "创作者": list(e["payload"]["创作者"]),
                                 "日期": e["date"],
                                 "版本事件": e["id"]})
            elif e["type"] == "许可授权":
                permissions.append(e["payload"])
            elif e["type"] == "公开演出":
                performances.append(e["payload"])
        return {
            "剧目": play_id,
            "剧名": head["payload"]["剧名"],
            "传统剧目": head["payload"]["传统剧目"],
            "版本": versions,
            "创作者": sorted(creators),
            "许可": permissions,
            "演出": performances,
        }

    def list_students(self):
        return [dict(self._first(f"stu:{e['payload']['学徒']}", "学徒建档")["payload"],
                     当前状态=self.student_view(e["payload"]["学徒"])["阶段水平"])
                for e in self.store.events if e["type"] == "学徒建档"]

    def list_plays(self):
        return [self.play_view(e["payload"]["剧目"])
                for e in self.store.events if e["type"] == "剧目立项"]
