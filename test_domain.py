"""领域不变量测试：覆盖藏品养护、两人批准、学徒范围、历程留痕、版本许可与幂等同步。"""

import tempfile
import unittest
from pathlib import Path

from domain import (
    HeritageSystem, DomainError, NotFoundError, ConflictError, STAGE_NAMES,
)


class HeritageCase(unittest.TestCase):
    def setUp(self):
        self.s = HeritageSystem()
        # 三名责任人：教师（操控、道具修缮）、管理员、传承人（唱腔、雕刻）
        self.s.register_person(person_id="t1", name="张教师", role="授课教师",
                               skills=["操控", "道具修缮"])
        self.s.register_person(person_id="g1", name="李管理", role="藏品管理员")
        self.s.register_person(person_id="c1", name="王传承", role="传承人",
                               skills=["唱腔", "雕刻"])
        # 一件珍贵原件、一件普通原件、一件复制品
        self.s.register_artifact(code="P001", name="明代关公皮影", category="皮影",
                                 tier="珍贵原件", registered_by="g1")
        self.s.register_artifact(code="P002", name="清代铜锣", category="乐器",
                                 tier="普通原件", registered_by="g1")
        self.s.register_artifact(code="R001", name="关公练功复制品", category="皮影",
                                 tier="复制品", replica_of="P001", registered_by="g1")

    def loan(self, code="P001", purpose="教学", approvers=("g1", "c1")):
        return self.s.create_loan(artifact_code=code, purpose=purpose,
                                  borrower_id="t1", approvers=list(approvers))["loan"]


class TestArtifactIdentity(HeritageCase):
    def test_code_unique_and_never_reused(self):
        with self.assertRaises(ConflictError):
            self.s.register_artifact(code="P001", name="重复编号", category="皮影",
                                     tier="普通原件")

    def test_replica_must_reference_original(self):
        with self.assertRaises(DomainError):
            self.s.register_artifact(code="R002", name="无主复制品", category="皮影",
                                     tier="复制品")
        with self.assertRaises(DomainError):
            self.s.register_artifact(code="R003", name="复制复制品", category="皮影",
                                     tier="复制品", replica_of="R001")

    def test_unknown_artifact(self):
        with self.assertRaises(NotFoundError):
            self.s.artifact_detail("NOPE")


class TestContinuousRecords(HeritageCase):
    def test_full_conservation_cycle(self):
        self.s.report_disease(artifact_code="P001", description="左臂开裂",
                              photos=["a.jpg", "b.jpg"], reporter="g1")
        self.assertEqual(self.s.artifacts["P001"]["state"], "限制使用")
        self.s.start_repair(artifact_code="P001", plan="驴皮内衬加固",
                            before_state="裂痕长2cm", repairer="t1")
        self.assertEqual(self.s.artifacts["P001"]["state"], "养护中")
        # 修复结束必须记录材料与修复后状态
        with self.assertRaises(DomainError):
            self.s.finish_repair(artifact_code="P001", materials_used=[], after_state="x")
        result = self.s.finish_repair(
            artifact_code="P001", materials_used=["驴皮", "鱼鳔胶"],
            after_state="裂痕闭合，活动正常", repairer="t1")
        self.assertEqual(result["state"], "在库")
        detail = self.s.artifact_detail("P001")
        self.assertEqual(detail["repairs"][0]["materials_used"], ["驴皮", "鱼鳔胶"])
        self.assertTrue(all(d["resolved"] for d in detail["diseases"]))

    def test_repair_requires_plan_and_before_state(self):
        with self.assertRaises(DomainError):
            self.s.start_repair(artifact_code="P002", plan=None, before_state=None)

    def test_records_append_only_ordered(self):
        self.s.add_inspection(location="主库房", temperature=21.5, humidity=55,
                              inspector="g1", note="通风", date="2026-03-01")
        self.s.conserve(artifact_code="P001", action="除尘", operator="g1",
                        date="2026-03-02")
        self.s.report_disease(artifact_code="P001", description="霉点",
                              reporter="g1", date="2026-03-03")
        timeline = self.s.artifact_detail("P001")["timeline"]
        kinds = [(t["date"], t["kind"]) for t in timeline]
        self.assertEqual(kinds, sorted(kinds))
        self.assertIn("环境巡检", [t["kind"] for t in timeline])


class TestTwoApproverRule(HeritageCase):
    def test_precious_original_needs_two_distinct_approvers(self):
        with self.assertRaises(DomainError):
            self.s.create_loan(artifact_code="P001", purpose="教学",
                               borrower_id="t1", approvers=["g1"])
        with self.assertRaises(DomainError):
            # 同一人填报两次不算两人
            self.s.create_loan(artifact_code="P001", purpose="教学",
                               borrower_id="t1", approvers=["g1", "g1"])
        loan = self.loan()
        self.assertEqual(len(set(loan["approvers"])), 2)
        self.assertEqual(self.s.artifacts["P001"]["state"], "教学借用")

    def test_normal_original_one_approver_suffices(self):
        loan = self.s.create_loan(artifact_code="P002", purpose="教学",
                                  borrower_id="t1", approvers=["g1"])["loan"]
        self.assertTrue(loan["approvers"])

    def test_open_loan_blocks_reloan_and_conservation(self):
        self.loan()
        with self.assertRaises(ConflictError):
            self.loan()
        with self.assertRaises(ConflictError):
            self.s.conserve(artifact_code="P001", action="除尘", operator="g1")

    def test_return_then_reloan_allowed(self):
        loan = self.loan()
        self.s.return_loan(loan_id=loan["id"], return_condition="完好")
        self.assertEqual(self.s.artifacts["P001"]["state"], "在库")
        with self.assertRaises(ConflictError):
            self.s.return_loan(loan_id=loan["id"], return_condition="再次归还")
        self.loan()

    def test_disease_found_on_loan_restricted_on_return(self):
        loan = self.loan()
        # 借期内登记病害不强行改借出状态；归还时转为限制使用
        self.s.report_disease(artifact_code="P001", description="演出中刮损",
                              reporter="g1", restrict=False)
        self.assertEqual(self.s.artifacts["P001"]["state"], "教学借用")
        result = self.s.return_loan(loan_id=loan["id"], return_condition="有刮损")
        self.assertEqual(result["state"], "限制使用")

    def test_precious_original_lesson_requires_approved_teaching_loan(self):
        self.s.register_apprentice(apprentice_id="a9", name="高手",
                                   birth_date="2000-01-01", mentor_id="t1")
        for idx in range(4):  # 逐级出师
            self.s.certify_skill(apprentice_id="a9", skill="操控",
                                 stage_index=idx, certifier_id="t1")
        with self.assertRaises(DomainError):  # 无教学借用
            self.s.record_lesson(mentor_id="t1", skill="操控",
                                 apprentice_ids=["a9"], artifact_code="P001")
        self.loan("P001")
        lesson = self.s.record_lesson(mentor_id="t1", skill="操控",
                                      apprentice_ids=["a9"], artifact_code="P001")
        self.assertEqual(lesson["lesson"]["artifact_tier"], "珍贵原件")


class TestApprenticeScope(HeritageCase):
    def setUp(self):
        super().setUp()
        # 固定在 2026-09-01 判定年龄：2010 年生 = 15 岁，2018 年生 = 8 岁，2021 年生 = 5 岁
        self.on = "2026-09-01"
        self.s.register_apprentice(apprentice_id="a1", name="小明",
                                   birth_date="2010-01-01", mentor_id="t1")

    def test_age_gate(self):
        self.s.register_apprentice(apprentice_id="a2", name="小童",
                                   birth_date="2021-01-01", mentor_id="t1")
        verdict = self.s.can_operate("a2", "R001", "操控", on=self.on)
        self.assertFalse(verdict["allowed"])
        self.assertTrue(any("年龄" in r for r in verdict["reasons"]))

    def test_skill_age_thresholds(self):
        # 8 岁可操控但不可雕刻（12 岁门槛）
        self.s.register_apprentice(apprentice_id="a3", name="八岁",
                                   birth_date="2018-01-01", mentor_id="t1")
        self.s.certify_skill(apprentice_id="a3", skill="操控", stage_index=0,
                             certifier_id="t1")
        self.assertTrue(self.s.can_operate("a3", "R001", "操控", on=self.on)["allowed"])
        verdict = self.s.can_operate("a3", "R001", "雕刻", on=self.on)
        self.assertFalse(verdict["allowed"])

    def test_stage_gate_for_originals(self):
        # 初学只能用复制品
        self.s.certify_skill(apprentice_id="a1", skill="操控", stage_index=0,
                             certifier_id="t1")
        self.assertTrue(self.s.can_operate("a1", "R001", "操控", on=self.on)["allowed"])
        verdict = self.s.can_operate("a1", "P002", "操控", on=self.on)
        self.assertFalse(verdict["allowed"])
        self.assertTrue(any("进阶" in r for r in verdict["reasons"]))
        # 进阶后可操作普通原件
        self.s.certify_skill(apprentice_id="a1", skill="操控", stage_index=1,
                             certifier_id="t1")
        self.assertTrue(self.s.can_operate("a1", "P002", "操控", on=self.on)["allowed"])

    def test_certification_must_progress_step_by_step(self):
        with self.assertRaises(DomainError):
            self.s.certify_skill(apprentice_id="a1", skill="操控", stage_index=2,
                                 certifier_id="t1")
        self.s.certify_skill(apprentice_id="a1", skill="操控", stage_index=0,
                             certifier_id="t1")
        with self.assertRaises(ConflictError):
            self.s.certify_skill(apprentice_id="a1", skill="操控", stage_index=0,
                                 certifier_id="t1")

    def test_mentor_must_cover_skill(self):
        verdict = self.s.can_operate("a1", "R001", "唱腔", on=self.on)
        self.assertFalse(verdict["allowed"])
        self.assertTrue(any("师承" in r for r in verdict["reasons"]))

    def test_original_into_class_requires_teaching_loan(self):
        self.s.certify_skill(apprentice_id="a1", skill="操控", stage_index=0,
                             certifier_id="t1")
        self.s.certify_skill(apprentice_id="a1", skill="操控", stage_index=1,
                             certifier_id="t1")
        with self.assertRaises(DomainError):
            self.s.record_lesson(mentor_id="t1", skill="操控",
                                 apprentice_ids=["a1"], artifact_code="P002")
        self.s.create_loan(artifact_code="P002", purpose="教学",
                           borrower_id="t1", approvers=["g1"])
        # 演出借用不能用于课堂
        self.assertTrue(self.s.record_lesson(
            mentor_id="t1", skill="操控", apprentice_ids=["a1"],
            artifact_code="P002")["ok"])

    def test_overlevel_lesson_rejected(self):
        self.s.certify_skill(apprentice_id="a1", skill="操控", stage_index=0,
                             certifier_id="t1")
        with self.assertRaises(DomainError):
            self.s.record_lesson(mentor_id="t1", skill="操控",
                                 apprentice_ids=["a1"], artifact_code="P002")


class TestApprenticeHistory(HeritageCase):
    def setUp(self):
        super().setUp()
        self.s.register_apprentice(apprentice_id="a1", name="小明",
                                   birth_date="2010-01-01", mentor_id="t1")
        self.s.certify_skill(apprentice_id="a1", skill="操控", stage_index=0,
                             certifier_id="t1")

    def test_absence_transfer_and_failed_review_are_append_only(self):
        self.s.record_absence(apprentice_id="a1", reason="生病")
        self.s.change_mentor(apprentice_id="a1", new_mentor_id="c1", reason="改学唱腔")
        self.s.review_work(apprentice_id="a1", work_ref="W1", skill="操控",
                           passed=False, reviewer_id="t1", comment="手腕太紧")
        detail = self.s.apprentice_detail("a1")
        kinds = [e["kind"] for e in detail["events"]]
        self.assertEqual(kinds, ["入门", "阶段通过", "缺课", "转师", "作品复核"])
        # 复核不通过不撤销已有阶段；缺课与转师也不抹去历程
        self.assertEqual(detail["apprentice"]["certifications"]["操控"], 0)
        self.assertEqual(detail["apprentice"]["current_mentor_id"], "c1")
        failed = detail["reviews"][0]["detail"]
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["reviewer_name"], "张教师")

    def test_cannot_transfer_to_same_mentor(self):
        with self.assertRaises(ConflictError):
            self.s.change_mentor(apprentice_id="a1", new_mentor_id="t1")

    def test_trace_lesson_to_mentor_and_actual_object(self):
        self.s.record_lesson(mentor_id="t1", skill="操控", apprentice_ids=["a1"],
                             artifact_code="R001", note="抖杆基础")
        detail = self.s.apprentice_detail("a1")
        lesson = detail["lessons"][0]
        self.assertEqual(lesson["mentor_name"], "张教师")
        self.assertEqual(lesson["artifact_code"], "R001")
        self.assertEqual(lesson["artifact_tier"], "复制品")
        self.assertEqual(lesson["replica_of"], "P001")


class TestPlayVersionsAndPerformance(HeritageCase):
    def setUp(self):
        super().setUp()
        self.s.register_play(play_id="PL1", title="空城计新编", play_type="新编",
                             creator="王传承")

    def add_version(self, version, state="已授权", expiry=None):
        return self.s.add_script_version(
            play_id="PL1", version=version, content_ref=f"doc-{version}",
            license_state=state, licensor="王传承", expiry=expiry)

    def test_pending_license_blocks_public_performance(self):
        self.add_version("v1", state="待授权")
        with self.assertRaises(DomainError):
            self.s.register_performance(
                performance_id="PF1", play_id="PL1", version="v1",
                artifact_codes=[], director_id="c1")

    def test_expired_license_blocks_performance(self):
        self.add_version("v1", expiry="2025-01-01")
        with self.assertRaises(DomainError):
            self.s.register_performance(
                performance_id="PF1", play_id="PL1", version="v1",
                artifact_codes=[], director_id="c1", date="2026-09-01")

    def test_licensed_performance_checks_artifact_loans(self):
        self.add_version("v2", expiry="2030-01-01")
        # 道具未经演出借用批准
        with self.assertRaises(DomainError):
            self.s.register_performance(
                performance_id="PF1", play_id="PL1", version="v2",
                artifact_codes=["P001"], director_id="c1")
        # 教学借用不能充演出
        self.loan("P001", purpose="教学")
        with self.assertRaises(DomainError):
            self.s.register_performance(
                performance_id="PF1", play_id="PL1", version="v2",
                artifact_codes=["P001"], director_id="c1")
        self.s.return_loan(
            loan_id=self.s.artifact_detail("P001")["loans"][-1]["id"],
            return_condition="完好")
        self.loan("P001", purpose="演出")
        result = self.s.register_performance(
            performance_id="PF1", play_id="PL1", version="v2",
            artifact_codes=["P001"], director_id="c1")
        self.assertEqual(result["performance"]["artifacts"][0]["approvers"], ["g1", "c1"])

    def test_duplicate_version_rejected(self):
        self.add_version("v1")
        with self.assertRaises(ConflictError):
            self.add_version("v1")


class TestIdempotentSync(HeritageCase):
    def test_direct_replay_is_idempotent(self):
        kwargs = dict(artifact_code="P001", action="通风除尘", operator="g1",
                      event_id="SCAN-1")
        first = self.s.conserve(**kwargs)
        replay = self.s.conserve(**kwargs)
        self.assertFalse(first["duplicate"])
        self.assertTrue(replay["duplicate"])
        self.assertEqual(len(self.s.collections["conservations"]), 1)

    def test_loan_not_double_booked_on_replay(self):
        kwargs = dict(artifact_code="P001", purpose="演出", borrower_id="t1",
                      approvers=["g1", "c1"], event_id="LOAN-1")
        self.s.create_loan(**kwargs)
        self.s.create_loan(**kwargs)  # 重放不应产生第二张借用单
        loans = self.s.collections["loans"]
        self.assertEqual(len(loans), 1)
        self.assertEqual(self.s.artifacts["P001"]["state"], "演出借用")

    def test_sync_batch_dedup_and_partial_failure(self):
        outcome = self.s.sync([
            {"type": "conserve", "artifact_code": "P001", "action": "除尘",
             "operator": "g1", "event_id": "E1"},
            {"type": "conserve", "artifact_code": "P001", "action": "除尘",
             "operator": "g1", "event_id": "E1"},                 # 批内重复
            {"type": "create_loan", "artifact_code": "P002", "purpose": "教学",
             "borrower_id": "t1", "approvers": ["g1"], "event_id": "E2"},
            {"type": "create_loan", "artifact_code": "P001", "purpose": "教学",
             "borrower_id": "t1", "approvers": ["g1"]},            # 业务失败：单人
            {"type": "unknown_kind"},                              # 未知类型
        ])
        self.assertEqual(outcome["synced"], 5)
        ok_flags = [r["ok"] for r in outcome["results"]]
        self.assertEqual(ok_flags, [True, True, True, False, False])
        self.assertTrue(outcome["results"][1]["duplicate"])
        self.assertEqual(len(self.s.collections["conservations"]), 1)
        self.assertEqual(len(self.s.collections["loans"]), 1)

    def test_replay_after_restart_still_dedup(self):
        self.s.conserve(artifact_code="P001", action="除尘", operator="g1",
                        event_id="E9")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.s.save(path)
            restored = HeritageSystem.load(path)
            result = restored.conserve(
                artifact_code="P001", action="除尘", operator="g1", event_id="E9")
            self.assertTrue(result["duplicate"])
            self.assertEqual(len(restored.collections["conservations"]), 1)


class TestTraceability(HeritageCase):
    def test_artifact_and_apprentice_bidirectional_trace(self):
        self.s.report_disease(artifact_code="P001", description="开裂", reporter="g1")
        self.s.start_repair(artifact_code="P001", plan="加固", before_state="开裂",
                            repairer="t1")
        self.s.finish_repair(artifact_code="P001", materials_used=["驴皮", "鱼鳔胶"],
                             after_state="修复", repairer="t1")
        self.loan("P001", purpose="教学")
        self.s.register_apprentice(apprentice_id="a1", name="小明",
                                   birth_date="2000-01-01", mentor_id="t1")
        for idx in range(4):
            self.s.certify_skill(apprentice_id="a1", skill="操控",
                                 stage_index=idx, certifier_id="t1")
        self.s.record_lesson(mentor_id="t1", skill="操控", apprentice_ids=["a1"],
                             artifact_code="P001")
        self.s.return_loan(
            loan_id=self.s.artifact_detail("P001")["loans"][-1]["id"],
            return_condition="完好")
        self.s.register_play(play_id="PL1", title="关公戏", play_type="传统")
        self.s.add_script_version(play_id="PL1", version="老本", content_ref="doc",
                                  license_state="已授权", licensor="传习所")
        self.loan("P001", purpose="演出")
        self.s.register_performance(performance_id="PF1", play_id="PL1",
                                    version="老本", artifact_codes=["P001"],
                                    director_id="c1")
        detail = self.s.artifact_detail("P001")
        # 保存状态、修复材料、使用责任、参演剧目一屏可见
        self.assertEqual(detail["artifact"]["state"], "演出借用")
        self.assertEqual(detail["repairs"][0]["materials_used"], ["驴皮", "鱼鳔胶"])
        self.assertEqual(detail["loans"][0]["approvers"], ["g1", "c1"])
        self.assertEqual(detail["performances"][0]["performance_id"], "PF1")
        kinds = {t["kind"] for t in detail["timeline"]}
        self.assertIn("公开演出", kinds)
        # 学徒成果追到指导者与实际练习对象
        apprentice = self.s.apprentice_detail("a1")
        self.assertEqual(apprentice["lessons"][0]["mentor_id"], "t1")
        self.assertEqual(apprentice["lessons"][0]["artifact_code"], "P001")
        self.assertEqual(apprentice["certifications"]["操控"]["stage"], STAGE_NAMES[3])


if __name__ == "__main__":
    unittest.main()
