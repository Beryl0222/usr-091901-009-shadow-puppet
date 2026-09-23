"""皮影传习与藏品养护领域层。

围绕领域契约 domain_contract.json 实现不可破坏的业务原则：
- 藏品稳定身份与只追加的连续记录；
- 珍贵原件两人批准方可进入课堂或演出；
- 学徒按年龄、师承、技法阶段获得可操作范围；
- 学徒历程只追加，缺课、转师、复核均不抹去；
- 剧目版本与创作者许可，公演前核对；
- 扫码事件幂等，断网恢复重放不重复记账。

所有写方法只接受关键字参数，并返回可 JSON 序列化的字典。
"""

import functools
import json
import threading
from datetime import date as date_cls
from pathlib import Path

CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")

ARTIFACT_TIERS = ("复制品", "普通原件", "珍贵原件")
ARTIFACT_CATEGORIES = ("皮影", "乐器", "剧本")
STATES = ("在库", "养护中", "教学借用", "演出借用", "限制使用")
SKILLS = ("操控", "唱腔", "雕刻", "道具修缮")
STAGE_NAMES = ("初学", "进阶", "熟练", "出师")
LICENSE_STATES = ("已授权", "待授权", "已过期")
LOAN_PURPOSES = {"教学": "教学借用", "演出": "演出借用"}
PERSON_ROLES = ("传承人", "藏品管理员", "授课教师", "学徒", "演出负责人")


class DomainError(Exception):
    """业务规则违反，携带机器可读错误码。"""

    def __init__(self, message, code="rule_violation"):
        super().__init__(message)
        self.code = code


class NotFoundError(DomainError):
    def __init__(self, message):
        super().__init__(message, "not_found")


class ConflictError(DomainError):
    def __init__(self, message):
        super().__init__(message, "conflict")


def _today():
    return date_cls.today().isoformat()


def _age_on(birth_date, on=None):
    """根据出生日期计算指定日期（默认今天）的周岁。"""
    born = date_cls.fromisoformat(birth_date)
    day = date_cls.fromisoformat(on) if on else date_cls.today()
    age = day.year - born.year - ((day.month, day.day) < (born.month, born.day))
    return age


def load_contract():
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    return contract


CONTRACT = load_contract()


def idempotent(command):
    """以 event_id 去重的命令装饰器：重放同一事件返回首次结果，不再记账。"""

    @functools.wraps(command)
    def wrapper(self, **kwargs):
        event_id = kwargs.get("event_id")
        with self.lock:
            if event_id is not None:
                cached = self.results_by_event.get(event_id)
                if cached is not None:
                    replay = dict(cached)
                    replay["duplicate"] = True
                    return replay
            result = command(self, **kwargs)
            if event_id is not None:
                result.setdefault("event_id", event_id)
                result.setdefault("duplicate", False)
                self.results_by_event[event_id] = dict(result)
            return result

    return wrapper


class HeritageSystem:
    """藏品养护与传习教学的领域服务（内存态，可做 JSON 快照持久化）。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.seq = 0
        self.event_log = []                 # 全局只追加事件日志
        self.results_by_event = {}          # event_id -> 首次结果（幂等）
        self.persons = {}                   # 责任人与教师
        self.artifacts = {}                 # 藏品道具
        self.apprentices = {}               # 学徒
        self.plays = {}                     # 剧目
        self.collections = {
            "inspections": [],
            "diseases": [],
            "conservations": [],
            "repairs": [],
            "loans": [],
            "lessons": [],
            "apprentice_events": [],
            "script_versions": [],
            "performances": [],
        }

    # ------------------------------------------------------------------ 基础

    def _emit(self, event_type, payload):
        self.seq += 1
        event = {"seq": self.seq, "type": event_type, "date": payload.get("date") or _today(),
                 "payload": payload}
        self.event_log.append(event)
        return event

    def _person(self, person_id):
        person = self.persons.get(person_id)
        if person is None:
            raise NotFoundError(f"责任人不存在：{person_id}")
        return person

    def _artifact(self, code):
        artifact = self.artifacts.get(code)
        if artifact is None:
            raise NotFoundError(f"藏品编号不存在：{code}")
        return artifact

    def _apprentice(self, apprentice_id):
        apprentice = self.apprentices.get(apprentice_id)
        if apprentice is None:
            raise NotFoundError(f"学徒不存在：{apprentice_id}")
        return apprentice

    def _play(self, play_id):
        play = self.plays.get(play_id)
        if play is None:
            raise NotFoundError(f"剧目不存在：{play_id}")
        return play

    def _transition(self, artifact, target, reason):
        # 每个目标状态允许的来源；“限制使用”可从任意状态进入
        sources = {
            "养护中": {"在库", "限制使用"},
            "在库": {"养护中", "教学借用", "演出借用", "限制使用"},
            "教学借用": {"在库"},
            "演出借用": {"在库"},
            "限制使用": set(STATES),
        }
        if artifact["state"] not in sources.get(target, set()):
            raise ConflictError(f"藏品 {artifact['code']} 状态 {artifact['state']} 不能转为 {target}：{reason}")
        artifact["state"] = target

    def _open_loan(self, code):
        for loan in reversed(self.collections["loans"]):
            if loan["artifact_code"] == code and loan["status"] == "借出":
                return loan
        return None

    # ------------------------------------------------------------------ 人员

    @idempotent
    def register_person(self, person_id=None, name=None, role=None, skills=None, date=None, event_id=None):
        """登记责任人、教师或演出负责人；skills 标明可授技法。"""
        if not person_id or not name:
            raise DomainError("人员编号与姓名必填")
        if person_id in self.persons:
            raise ConflictError(f"人员编号已存在：{person_id}")
        if role not in PERSON_ROLES:
            raise DomainError(f"角色必须是 {PERSON_ROLES} 之一")
        skills = list(skills or [])
        unknown = [s for s in skills if s not in SKILLS]
        if unknown:
            raise DomainError(f"未知技法：{unknown}")
        person = {"person_id": person_id, "name": name, "role": role, "skills": skills}
        self.persons[person_id] = person
        self._emit("person_registered", {"person": person, "date": date})
        return {"ok": True, "person": person}

    # ------------------------------------------------------------------ 藏品

    @idempotent
    def register_artifact(self, code=None, name=None, category=None, tier=None,
                          location="主库房", replica_of=None, registered_by=None, date=None, event_id=None):
        """登记藏品。编号一经注册永不复用；复制品须关联原件。"""
        if not code or not name:
            raise DomainError("藏品编号与名称必填")
        if code in self.artifacts:
            raise ConflictError(f"藏品编号已存在且永不复用：{code}")
        if category not in ARTIFACT_CATEGORIES:
            raise DomainError(f"类别必须是 {ARTIFACT_CATEGORIES} 之一")
        if tier not in ARTIFACT_TIERS:
            raise DomainError(f"等级必须是 {ARTIFACT_TIERS} 之一")
        if registered_by:
            self._person(registered_by)
        original = None
        if tier == "复制品":
            if not replica_of:
                raise DomainError("复制品必须关联原件编号 replica_of")
            original = self._artifact(replica_of)
            if original["tier"] == "复制品":
                raise DomainError("复制品不能再复制复制品")
        elif replica_of:
            raise DomainError("仅复制品可关联原件")
        artifact = {
            "code": code, "name": name, "category": category, "tier": tier,
            "location": location, "state": "在库", "replica_of": replica_of,
            "registered_by": registered_by, "open_disease": False,
        }
        self.artifacts[code] = artifact
        self._emit("artifact_registered", {"artifact": artifact, "date": date})
        return {"ok": True, "artifact": artifact}

    @idempotent
    def add_inspection(self, location=None, temperature=None, humidity=None,
                       note=None, inspector=None, date=None, event_id=None):
        """登记环境巡检（温湿度、通风除尘等环境层面记录）。"""
        if not location or inspector is None:
            raise DomainError("巡检库位与巡检人必填")
        self._person(inspector)
        record = {
            "id": f"INS{len(self.collections['inspections']) + 1:04d}",
            "location": location, "temperature": temperature, "humidity": humidity,
            "note": note, "inspector": inspector, "date": date or _today(),
        }
        self.collections["inspections"].append(record)
        self._emit("inspection_added", record)
        return {"ok": True, "inspection": record}

    @idempotent
    def report_disease(self, artifact_code=None, description=None, photos=None,
                       reporter=None, restrict=True, date=None, event_id=None):
        """登记病害与照片（只追加）。默认同时将在库原件转为限制使用。"""
        artifact = self._artifact(artifact_code)
        if not description:
            raise DomainError("病害描述必填")
        if reporter:
            self._person(reporter)
        record = {
            "id": f"DIS{len(self.collections['diseases']) + 1:04d}",
            "artifact_code": artifact_code, "description": description,
            "photos": list(photos or []), "reporter": reporter,
            "date": date or _today(), "resolved": False,
        }
        self.collections["diseases"].append(record)
        artifact["open_disease"] = True
        if restrict and artifact["state"] == "在库":
            self._transition(artifact, "限制使用", "发现病害")
        self._emit("disease_reported", record)
        return {"ok": True, "disease": record, "state": artifact["state"]}

    @idempotent
    def conserve(self, artifact_code=None, action=None, note=None,
                 operator=None, date=None, event_id=None):
        """登记日常养护（通风除尘等）。借用中或修复中的藏品不在库房，不得养护。"""
        artifact = self._artifact(artifact_code)
        if not action:
            raise DomainError("养护动作必填")
        if operator:
            self._person(operator)
        if artifact["state"] not in ("在库", "限制使用"):
            raise ConflictError(f"藏品处于 {artifact['state']}，不在库房，无法养护")
        record = {
            "id": f"CON{len(self.collections['conservations']) + 1:04d}",
            "artifact_code": artifact_code, "action": action, "note": note,
            "operator": operator, "date": date or _today(),
        }
        self.collections["conservations"].append(record)
        self._emit("conservation_done", record)
        return {"ok": True, "conservation": record}

    @idempotent
    def start_repair(self, artifact_code=None, plan=None, materials_planned=None,
                     before_state=None, repairer=None, date=None, event_id=None):
        """开始修复：记录方案与修复前状态，在库/限制使用转为养护中。"""
        artifact = self._artifact(artifact_code)
        if not plan or not before_state:
            raise DomainError("修复方案与修复前状态必填")
        if repairer:
            self._person(repairer)
        if artifact["state"] not in ("在库", "限制使用"):
            raise ConflictError(f"藏品处于 {artifact['state']}，无法开始修复")
        record = {
            "id": f"REP{len(self.collections['repairs']) + 1:04d}",
            "artifact_code": artifact_code, "plan": plan,
            "materials_planned": list(materials_planned or []),
            "before_state": before_state, "repairer": repairer,
            "start_date": date or _today(), "status": "修复中",
            "materials_used": None, "after_state": None, "finish_date": None, "result": None,
        }
        self.collections["repairs"].append(record)
        self._transition(artifact, "养护中", "开始修复")
        self._emit("repair_started", {k: record[k] for k in
                    ("id", "artifact_code", "plan", "before_state", "start_date")})
        return {"ok": True, "repair": record}

    @idempotent
    def finish_repair(self, artifact_code=None, materials_used=None, after_state=None,
                      result="完成", repairer=None, date=None, event_id=None):
        """结束修复：必须记录所用材料与修复后状态；完成后解除病害限制。"""
        artifact = self._artifact(artifact_code)
        if not materials_used or not after_state:
            raise DomainError("修复所用材料与修复后状态必填")
        if result not in ("完成", "未完成"):
            raise DomainError("修复结果只能是 完成/未完成")
        open_repair = None
        for repair in reversed(self.collections["repairs"]):
            if repair["artifact_code"] == artifact_code and repair["status"] == "修复中":
                open_repair = repair
                break
        if open_repair is None:
            raise ConflictError("该藏品没有进行中的修复")
        open_repair.update({
            "materials_used": list(materials_used), "after_state": after_state,
            "finish_date": date or _today(), "result": result, "status": result,
        })
        if result == "完成":
            artifact["open_disease"] = False
            for disease in self.collections["diseases"]:
                if disease["artifact_code"] == artifact_code and not disease["resolved"]:
                    disease["resolved"] = True
            self._transition(artifact, "在库", "修复完成")
        else:
            self._transition(artifact, "限制使用", "修复未完成，继续限制")
        self._emit("repair_finished", {k: open_repair[k] for k in
                    ("id", "artifact_code", "materials_used", "after_state", "result", "finish_date")})
        return {"ok": True, "repair": open_repair, "state": artifact["state"]}

    # ------------------------------------------------------------------ 借用

    @idempotent
    def create_loan(self, artifact_code=None, purpose=None, borrower_id=None,
                    approvers=None, usage_ref=None, note=None, date=None, event_id=None):
        """领用批准。珍贵原件须两名不同责任人批准，其他至少一人。"""
        artifact = self._artifact(artifact_code)
        if purpose not in LOAN_PURPOSES:
            raise DomainError("用途必须是 教学/演出")
        borrower = self._person(borrower_id) if borrower_id else None
        approvers = list(approvers or [])
        if not approvers:
            raise DomainError("至少需要一名批准人")
        for approver in approvers:
            self._person(approver)
        if len(set(approvers)) != len(approvers):
            raise DomainError("批准人不得重复填报")
        if artifact["tier"] == "珍贵原件" and len(approvers) < 2:
            raise DomainError("珍贵原件进入课堂或演出必须经两名不同责任人批准")
        if artifact["state"] != "在库":
            raise ConflictError(f"藏品当前状态为 {artifact['state']}，不能领用")
        if self._open_loan(artifact_code) is not None:
            raise ConflictError("该藏品存在未归还的借用")
        loan = {
            "id": f"LOAN{len(self.collections['loans']) + 1:04d}",
            "artifact_code": artifact_code, "purpose": purpose,
            "borrower_id": borrower_id, "borrower_name": borrower["name"] if borrower else None,
            "approvers": approvers, "usage_ref": usage_ref, "note": note,
            "start_date": date or _today(), "status": "借出",
            "return_date": None, "return_condition": None,
        }
        self.collections["loans"].append(loan)
        self._transition(artifact, LOAN_PURPOSES[purpose], "领用批准完成")
        self._emit("loan_approved", loan)
        return {"ok": True, "loan": loan}

    @idempotent
    def return_loan(self, loan_id=None, return_condition=None, returned_by=None,
                    date=None, event_id=None):
        """归还并核验状态；借用期间挂起的病害使藏品进入限制使用。"""
        loan = next((l for l in self.collections["loans"] if l["id"] == loan_id), None)
        if loan is None:
            raise NotFoundError(f"借用单不存在：{loan_id}")
        if loan["status"] != "借出":
            raise ConflictError(f"借用单 {loan_id} 已归还")
        artifact = self._artifact(loan["artifact_code"])
        if not return_condition:
            raise DomainError("归还时必须填写状态核验结论")
        loan.update({"status": "已还", "return_date": date or _today(),
                     "return_condition": return_condition, "returned_by": returned_by})
        if artifact["open_disease"]:
            artifact["state"] = "限制使用"
        else:
            self._transition(artifact, "在库", "归还核验")
        self._emit("loan_returned", {k: loan[k] for k in
                    ("id", "artifact_code", "return_date", "return_condition")})
        return {"ok": True, "loan": loan, "state": artifact["state"]}

    # ------------------------------------------------------------------ 学徒

    @idempotent
    def register_apprentice(self, apprentice_id=None, name=None, birth_date=None,
                            mentor_id=None, date=None, event_id=None):
        """登记学徒及其师承；出生日期用于持续按年龄判定操作范围。"""
        if not apprentice_id or not name or not birth_date:
            raise DomainError("学徒编号、姓名、出生日期必填")
        if apprentice_id in self.apprentices:
            raise ConflictError(f"学徒编号已存在：{apprentice_id}")
        date_cls.fromisoformat(birth_date)  # 校验格式
        mentor = self._person(mentor_id) if mentor_id else None
        if mentor and not mentor["skills"]:
            raise DomainError(f"{mentor['name']} 未登记可授技法，不能作为指导教师")
        apprentice = {
            "apprentice_id": apprentice_id, "name": name, "birth_date": birth_date,
            "current_mentor_id": mentor_id, "certifications": {},
            "registered_date": date or _today(),
        }
        self.apprentices[apprentice_id] = apprentice
        self._append_apprentice_event(apprentice, "入门", date or _today(),
                                      {"mentor_id": mentor_id}, mentor_id)
        self._emit("apprentice_registered", {"apprentice": apprentice})
        return {"ok": True, "apprentice": apprentice}

    def _append_apprentice_event(self, apprentice, kind, on, detail, mentor_id=None):
        record = {
            "id": f"AE{len(self.collections['apprentice_events']) + 1:04d}",
            "apprentice_id": apprentice["apprentice_id"], "kind": kind,
            "date": on, "mentor_id": mentor_id or apprentice["current_mentor_id"],
            "detail": detail,
        }
        self.collections["apprentice_events"].append(record)
        self._emit("apprentice_event", record)
        return record

    def can_operate(self, apprentice_id, artifact_code, skill, on=None):
        """按年龄、师承、已通过技法阶段与藏品等级判定可操作范围。"""
        apprentice = self._apprentice(apprentice_id)
        artifact = self._artifact(artifact_code)
        reasons = []
        if skill not in SKILLS:
            raise DomainError(f"未知技法：{skill}")
        age = _age_on(apprentice["birth_date"], on)
        min_age = CONTRACT["skill_rules"][skill]["min_age"]
        if age < min_age:
            reasons.append(f"年龄 {age} 岁低于 {skill} 的最低年龄 {min_age} 岁")
        stage = apprentice["certifications"].get(skill, -1)
        min_stage = CONTRACT["tier_stage_rules"][artifact["tier"]]["min_stage_index"]
        if stage < min_stage:
            required = STAGE_NAMES[min_stage] if min_stage >= 0 else "无门槛"
            reached = STAGE_NAMES[stage] if stage >= 0 else "尚未入门"
            reasons.append(f"{skill} 阶段为 {reached}，{artifact['tier']} 要求 {required}")
        mentor_id = apprentice["current_mentor_id"]
        mentor = self.persons.get(mentor_id) if mentor_id else None
        if mentor is None or skill not in mentor["skills"]:
            reasons.append("当前师承不覆盖该技法")
        return {"allowed": not reasons, "reasons": reasons, "age": age,
                "skill_stage": stage, "tier": artifact["tier"]}

    @idempotent
    def certify_skill(self, apprentice_id=None, skill=None, stage_index=None,
                      certifier_id=None, work_ref=None, date=None, event_id=None):
        """认定学徒通过某技法阶段，只能逐级通过。"""
        apprentice = self._apprentice(apprentice_id)
        if skill not in SKILLS:
            raise DomainError(f"未知技法：{skill}")
        if not isinstance(stage_index, int) or not 0 <= stage_index < len(STAGE_NAMES):
            raise DomainError("阶段序号必须在 0..3")
        certifier = self._person(certifier_id)
        if skill not in certifier["skills"]:
            raise DomainError(f"{certifier['name']} 不具备 {skill} 认定资格")
        current = apprentice["certifications"].get(skill, -1)
        if stage_index <= current:
            raise ConflictError(f"已通过 {STAGE_NAMES[current] if current >= 0 else '入门前'} 阶段，不得重复或倒退登记")
        if stage_index != current + 1:
            raise DomainError(f"技法阶段必须逐级通过，当前应登记 {STAGE_NAMES[current + 1]}")
        apprentice["certifications"][skill] = stage_index
        self._append_apprentice_event(
            apprentice, "阶段通过", date or _today(),
            {"skill": skill, "stage_index": stage_index, "stage": STAGE_NAMES[stage_index],
             "certifier_id": certifier_id, "work_ref": work_ref},
            certifier_id)
        return {"ok": True, "apprentice_id": apprentice_id, "skill": skill,
                "stage": STAGE_NAMES[stage_index]}

    @idempotent
    def record_absence(self, apprentice_id=None, reason=None, date=None, event_id=None):
        """登记缺课，只追加，不影响已有学习历程。"""
        apprentice = self._apprentice(apprentice_id)
        if not reason:
            raise DomainError("缺课原因必填")
        record = self._append_apprentice_event(
            apprentice, "缺课", date or _today(), {"reason": reason})
        return {"ok": True, "event": record}

    @idempotent
    def change_mentor(self, apprentice_id=None, new_mentor_id=None,
                      reason=None, date=None, event_id=None):
        """转师：更新当前师承，完整保留师承历史。"""
        apprentice = self._apprentice(apprentice_id)
        new_mentor = self._person(new_mentor_id)
        if new_mentor_id == apprentice["current_mentor_id"]:
            raise ConflictError("新导师与当前导师相同")
        if not new_mentor["skills"]:
            raise DomainError("新导师未登记可授技法")
        old_mentor = apprentice["current_mentor_id"]
        apprentice["current_mentor_id"] = new_mentor_id
        record = self._append_apprentice_event(
            apprentice, "转师", date or _today(),
            {"from_mentor_id": old_mentor, "to_mentor_id": new_mentor_id, "reason": reason},
            new_mentor_id)
        return {"ok": True, "event": record}

    @idempotent
    def review_work(self, apprentice_id=None, work_ref=None, skill=None, passed=None,
                    reviewer_id=None, comment=None, date=None, event_id=None):
        """作品复核：无论通过与否都只追加，不抹去既有学习历程。"""
        apprentice = self._apprentice(apprentice_id)
        if not work_ref or skill not in SKILLS or passed is None:
            raise DomainError("作品编号、技法与复核结论必填")
        reviewer = self._person(reviewer_id)
        record = self._append_apprentice_event(
            apprentice, "作品复核", date or _today(),
            {"work_ref": work_ref, "skill": skill, "passed": bool(passed),
             "reviewer_id": reviewer_id, "reviewer_name": reviewer["name"],
             "comment": comment}, reviewer_id)
        return {"ok": True, "event": record}

    @idempotent
    def record_lesson(self, mentor_id=None, skill=None, apprentice_ids=None,
                      artifact_code=None, date=None, note=None, event_id=None):
        """登记授课练习：指导者、学徒与实际练习对象（原件或复制品）全部留痕。"""
        mentor = self._person(mentor_id)
        if skill not in mentor["skills"]:
            raise DomainError(f"{mentor['name']} 不能教授 {skill}")
        apprentice_ids = list(apprentice_ids or [])
        if not apprentice_ids:
            raise DomainError("至少一名学徒参与")
        artifact = self._artifact(artifact_code)
        on = date or _today()
        for apprentice_id in apprentice_ids:
            verdict = self.can_operate(apprentice_id, artifact_code, skill, on=on)
            if not verdict["allowed"]:
                raise DomainError(
                    f"学徒 {apprentice_id} 不可操作 {artifact_code}：{'；'.join(verdict['reasons'])}")
        # 原件进课堂须有有效教学借用；珍贵原件的借用已在批准环节强制两人批准
        if artifact["tier"] in ("普通原件", "珍贵原件"):
            loan = self._open_loan(artifact_code)
            if loan is None or loan["purpose"] != "教学":
                raise DomainError(f"原件 {artifact_code} 未经教学领用批准，不得进入课堂")
            if artifact["tier"] == "珍贵原件" and len(set(loan["approvers"])) < 2:
                raise DomainError("珍贵原件的教学借用必须有两名责任人批准")
        lesson = {
            "id": f"LES{len(self.collections['lessons']) + 1:04d}",
            "mentor_id": mentor_id, "mentor_name": mentor["name"], "skill": skill,
            "apprentice_ids": apprentice_ids, "artifact_code": artifact_code,
            "artifact_tier": artifact["tier"], "date": on, "note": note,
        }
        self.collections["lessons"].append(lesson)
        self._emit("lesson_recorded", lesson)
        return {"ok": True, "lesson": lesson}

    # ------------------------------------------------------------------ 剧目

    @idempotent
    def register_play(self, play_id=None, title=None, play_type=None,
                      creator=None, date=None, event_id=None):
        """登记传统或新编剧目。"""
        if not play_id or not title:
            raise DomainError("剧目编号与名称必填")
        if play_id in self.plays:
            raise ConflictError(f"剧目编号已存在：{play_id}")
        if play_type not in ("传统", "新编"):
            raise DomainError("剧目类型必须是 传统/新编")
        play = {"play_id": play_id, "title": title, "type": play_type,
                "creator": creator, "registered_date": date or _today()}
        self.plays[play_id] = play
        self._emit("play_registered", play)
        return {"ok": True, "play": play}

    @idempotent
    def add_script_version(self, play_id=None, version=None, content_ref=None,
                           license_state=None, licensor=None, license_doc=None,
                           expiry=None, date=None, event_id=None):
        """为剧目添加唱词版本（含新编唱词）并登记创作者许可。"""
        play = self._play(play_id)
        if not version or not content_ref:
            raise DomainError("版本号与内容凭据必填")
        if license_state not in LICENSE_STATES:
            raise DomainError(f"许可状态必须是 {LICENSE_STATES} 之一")
        existing = {(v["play_id"], v["version"]) for v in self.collections["script_versions"]}
        if (play_id, version) in existing:
            raise ConflictError(f"剧目 {play_id} 的版本 {version} 已存在")
        record = {
            "play_id": play_id, "version": version, "content_ref": content_ref,
            "license_state": license_state, "licensor": licensor,
            "license_doc": license_doc, "expiry": expiry, "date": date or _today(),
        }
        self.collections["script_versions"].append(record)
        self._emit("script_version_added", record)
        return {"ok": True, "script_version": record}

    def _license_effective(self, version_record, on=None):
        """已授权且未过有效期方为有效。"""
        if version_record["license_state"] != "已授权":
            return False, f"许可状态为 {version_record['license_state']}"
        if version_record.get("expiry"):
            day = on or _today()
            if day > version_record["expiry"]:
                return False, f"许可已于 {version_record['expiry']} 过期"
        return True, None

    def _get_version(self, play_id, version):
        found = [v for v in self.collections["script_versions"]
                 if v["play_id"] == play_id and v["version"] == version]
        if not found:
            raise NotFoundError(f"剧目 {play_id} 不存在版本 {version}")
        return found[-1]

    @idempotent
    def register_performance(self, performance_id=None, play_id=None, version=None,
                             artifact_codes=None, director_id=None, date=None, event_id=None):
        """登记公开演出：核对版本许可，并逐件核对道具演出借用批准。"""
        play = self._play(play_id)
        version_record = self._get_version(play_id, version)
        on = date or _today()
        effective, license_reason = self._license_effective(version_record, on)
        if not effective:
            raise DomainError(f"剧目 {play['title']} 版本 {version} {license_reason}，不得公开演出")
        director = self._person(director_id) if director_id else None
        artifact_codes = list(artifact_codes or [])
        used = []
        for code in artifact_codes:
            artifact = self._artifact(code)
            loan = self._open_loan(code)
            if loan is None or loan["purpose"] != "演出":
                raise DomainError(f"道具 {code} 未经演出领用批准，不得带演")
            if artifact["tier"] == "珍贵原件" and len(set(loan["approvers"])) < 2:
                raise DomainError(f"珍贵原件 {code} 的演出借用必须有两名责任人批准")
            used.append({"artifact_code": code, "loan_id": loan["id"],
                         "approvers": loan["approvers"]})
        performance = {
            "performance_id": performance_id, "play_id": play_id,
            "title": play["title"], "version": version, "artifacts": used,
            "director_id": director_id, "director_name": director["name"] if director else None,
            "date": on,
        }
        if not performance_id:
            raise DomainError("演出编号必填")
        if any(p["performance_id"] == performance_id for p in self.collections["performances"]):
            raise ConflictError(f"演出编号已存在：{performance_id}")
        self.collections["performances"].append(performance)
        self._emit("performance_registered", performance)
        return {"ok": True, "performance": performance}

    # ------------------------------------------------------------------ 查询

    def artifact_detail(self, code):
        """一件道具看清：保存状态、病害、修复材料、使用责任与参演剧目。"""
        artifact = self._artifact(code)
        diseases = [d for d in self.collections["diseases"] if d["artifact_code"] == code]
        conservations = [c for c in self.collections["conservations"] if c["artifact_code"] == code]
        repairs = [r for r in self.collections["repairs"] if r["artifact_code"] == code]
        loans = [l for l in self.collections["loans"] if l["artifact_code"] == code]
        lessons = [l for l in self.collections["lessons"] if l["artifact_code"] == code]
        performances = [p for p in self.collections["performances"]
                        if any(a["artifact_code"] == code for a in p["artifacts"])]
        inspections = [i for i in self.collections["inspections"]
                       if i["location"] == artifact["location"]]
        timeline = []
        for i in inspections:
            timeline.append({"date": i["date"], "kind": "环境巡检", "ref": i["id"], "summary": i["note"]})
        for d in diseases:
            timeline.append({"date": d["date"], "kind": "病害", "ref": d["id"], "summary": d["description"]})
        for c in conservations:
            timeline.append({"date": c["date"], "kind": "养护", "ref": c["id"], "summary": c["action"]})
        for r in repairs:
            timeline.append({"date": r["start_date"], "kind": "修复开始", "ref": r["id"], "summary": r["plan"]})
            if r["finish_date"]:
                timeline.append({"date": r["finish_date"], "kind": "修复结束", "ref": r["id"],
                                 "summary": f"{r['result']}；材料：{'、'.join(r['materials_used'])}"})
        for l in loans:
            timeline.append({"date": l["start_date"], "kind": f"{l['purpose']}借用", "ref": l["id"],
                             "summary": f"借用人 {l['borrower_name']}；批准 {'、'.join(l['approvers'])}"})
            if l["return_date"]:
                timeline.append({"date": l["return_date"], "kind": "归还", "ref": l["id"],
                                 "summary": l["return_condition"]})
        for lesson in lessons:
            timeline.append({"date": lesson["date"], "kind": "教学练习", "ref": lesson["id"],
                             "summary": f"{lesson['skill']}，指导 {lesson['mentor_name']}"})
        for p in performances:
            timeline.append({"date": p["date"], "kind": "公开演出", "ref": p["performance_id"],
                             "summary": f"《{p['title']}》版本 {p['version']}"})
        timeline.sort(key=lambda item: (item["date"], item["ref"]))
        return {
            "artifact": artifact, "diseases": diseases, "inspections": inspections,
            "conservations": conservations, "repairs": repairs, "loans": loans,
            "lessons": lessons, "performances": performances, "timeline": timeline,
        }

    def apprentice_detail(self, apprentice_id):
        """一名学徒看清：师承、阶段、历程事件、成果复核、指导者与实际练习对象。"""
        apprentice = self._apprentice(apprentice_id)
        events = [e for e in self.collections["apprentice_events"]
                  if e["apprentice_id"] == apprentice_id]
        lessons = []
        for lesson in self.collections["lessons"]:
            if apprentice_id in lesson["apprentice_ids"]:
                artifact = self.artifacts.get(lesson["artifact_code"], {})
                lessons.append({
                    "lesson_id": lesson["id"], "date": lesson["date"], "skill": lesson["skill"],
                    "mentor_id": lesson["mentor_id"], "mentor_name": lesson["mentor_name"],
                    "artifact_code": lesson["artifact_code"],
                    "artifact_name": artifact.get("name"), "artifact_tier": lesson["artifact_tier"],
                    "replica_of": artifact.get("replica_of"),
                })
        reviews = [e for e in events if e["kind"] == "作品复核"]
        return {
            "apprentice": apprentice,
            "age": _age_on(apprentice["birth_date"]),
            "certifications": {skill: {"stage_index": idx, "stage": STAGE_NAMES[idx]}
                               for skill, idx in apprentice["certifications"].items()},
            "mentor_history": [e for e in events if e["kind"] in ("入门", "转师")],
            "events": events, "lessons": lessons, "reviews": reviews,
        }

    # ------------------------------------------------------ 断网批量同步与持久化

    DISPATCH = {
        "register_person": "register_person",
        "register_artifact": "register_artifact",
        "add_inspection": "add_inspection",
        "report_disease": "report_disease",
        "conserve": "conserve",
        "start_repair": "start_repair",
        "finish_repair": "finish_repair",
        "create_loan": "create_loan",
        "return_loan": "return_loan",
        "register_apprentice": "register_apprentice",
        "certify_skill": "certify_skill",
        "record_absence": "record_absence",
        "change_mentor": "change_mentor",
        "review_work": "review_work",
        "record_lesson": "record_lesson",
        "register_play": "register_play",
        "add_script_version": "add_script_version",
        "register_performance": "register_performance",
    }

    def sync(self, events):
        """离线扫码设备恢复联网后批量提交。

        每条事件携带稳定 event_id；重复事件（含本次重放与历史重放）只返回
        首次结果并标注 duplicate，绝不会重复记一次养护或借出。
        单条失败不影响其余事件，错误随条返回。
        """
        results = []
        for item in events:
            kind = item.get("type")
            payload = {k: v for k, v in item.items() if k != "type"}
            if kind not in self.DISPATCH:
                results.append({"type": kind, "ok": False, "error": "未知事件类型",
                                "code": "unknown_type"})
                continue
            try:
                outcome = getattr(self, self.DISPATCH[kind])(**payload)
                results.append({"type": kind, "ok": True, **outcome})
            except DomainError as error:
                results.append({"type": kind, "ok": False, "error": str(error), "code": error.code})
        return {"synced": len(events), "results": results}

    def to_dict(self):
        return {
            "seq": self.seq,
            "event_log": self.event_log,
            "results_by_event": self.results_by_event,
            "persons": self.persons,
            "artifacts": self.artifacts,
            "apprentices": self.apprentices,
            "plays": self.plays,
            **self.collections,
        }

    @classmethod
    def from_dict(cls, data):
        system = cls()
        system.seq = data.get("seq", 0)
        system.event_log = data.get("event_log", [])
        system.results_by_event = data.get("results_by_event", {})
        system.persons = data.get("persons", {})
        system.artifacts = data.get("artifacts", {})
        system.apprentices = data.get("apprentices", {})
        system.plays = data.get("plays", {})
        for key in system.collections:
            system.collections[key] = data.get(key, [])
        return system

    def save(self, path):
        path = Path(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path):
        path = Path(path)
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
