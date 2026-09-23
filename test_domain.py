"""领域规则测试：身份连续记录、双人批准、技法授权、版本许可、幂等与追溯。"""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from domain import DomainError, EventStore, HeritageService

TOMORROW = (date.today() + timedelta(days=1)).isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
OLD_DATE = "2020-01-01"

TECHNIQUES = [
    {"名称": "操影启蒙", "阶段": "启蒙", "前置技法": []},
    {"名称": "唱腔入门", "阶段": "入门", "前置技法": ["操影启蒙"]},
    {"名称": "影偶修缮", "阶段": "熟练", "前置技法": ["唱腔入门"]},
]


def build_world(**kwargs):
    """构造一个带目录、人员、原件与复制品的标准传习所。"""
    svc = HeritageService(EventStore(**kwargs))
    svc.seed_skill_catalog(TECHNIQUES)
    for pid, name, roles in [
        ("per_tan", "谭师傅", ["传承人", "藏品管理员"]),
        ("per_li", "李师傅", ["传承人"]),
        ("per_wang", "王老师", ["授课教师"]),
    ]:
        svc.register_person(pid, name, roles)
    original = svc.register_object(
        "皮影", "明代关公影偶", precious=True, object_id="obj_old",
        acquired="1590-03-12", 登记人="per_tan")[0]
    replica = svc.add_replica(
        "obj_old", "关公影偶教学复制品", replica_id="obj_copy",
        maker="per_wang")[0]
    return svc, original, replica


class ObjectLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.svc, _, _ = build_world()

    def test_stable_identity_and_replica_link(self):
        obj = self.svc.object_view("obj_old")
        self.assertEqual(obj["身份"]["物件"], "obj_old")
        self.assertTrue(obj["身份"]["珍贵原件"])
        self.assertEqual(self.svc.object_state("obj_old"), "在库")
        copy = self.svc.object_view("obj_copy")
        self.assertFalse(copy["身份"]["珍贵原件"])
        self.assertEqual(copy["身份"]["原件"], "obj_old")

    def test_identity_cannot_be_registered_twice(self):
        with self.assertRaises(DomainError):
            self.svc.register_object("皮影", "冒名影偶", object_id="obj_old")
        with self.assertRaises(DomainError):
            self.svc.add_replica("obj_old", "冒名复制品",
                                 replica_id="obj_copy")
        # 首次登记带幂等键，断网重放同一键+ID 返回首次事件而非报错
        self.svc.register_object(
            "道具", "签到木牌", object_id="obj_z", idem="register-z")
        event, created = self.svc.register_object(
            "道具", "签到木牌", object_id="obj_z", idem="register-z")
        self.assertFalse(created)
        self.assertEqual(event["payload"]["物件"], "obj_z")

    def test_inspection_disease_and_repair_form_continuous_record(self):
        self.svc.record_environment(
            "obj_old", 21.4, 55, "异常", "per_tan",
            photo="dust-001.jpg", note="关节积尘")
        self.svc.attach_disease_photo(
            "obj_old", "crack-001.jpg", "颈部发丝裂纹", "per_tan")
        self.svc.start_repair(
            "obj_old", "通风除尘并粘合颈部", ["软毛刷", "糯米浆"], "per_li")
        self.assertEqual(self.svc.object_state("obj_old"), "养护中")
        self.svc.finish_repair(
            "obj_old", ["软毛刷", "糯米浆", "桑皮纸"], "per_li",
            note="活动度恢复")
        view = self.svc.object_view("obj_old")
        self.assertEqual(view["当前状态"], "在库")
        self.assertEqual(view["修复材料"], ["桑皮纸", "糯米浆", "软毛刷"])
        repair = view["修复记录"][0]
        self.assertEqual(repair["开始"]["前状态"], "在库")
        self.assertEqual(repair["完成"]["后状态"], "在库")
        timeline = [e["事件"] for e in view["时间线"]]
        self.assertEqual(timeline.count("环境巡检"), 1)
        self.assertIn("病害记录", timeline)
        self.assertEqual(timeline.count("修复开始"), 1)
        self.assertEqual(timeline.count("修复完成"), 1)

    def test_repair_cannot_finish_twice(self):
        self.svc.start_repair("obj_old", "除尘", ["毛刷"], "per_li")
        self.svc.finish_repair("obj_old", ["毛刷"], "per_li")
        with self.assertRaises(DomainError):
            self.svc.finish_repair("obj_old", ["毛刷"], "per_li")

    def test_borrow_while_away_is_rejected(self):
        self.svc.borrow_object("obj_copy", "教学", "per_wang")
        with self.assertRaises(DomainError):
            self.svc.borrow_object("obj_copy", "教学", "per_wang")

    def test_damaged_return_marks_restricted(self):
        self.svc.borrow_object("obj_copy", "教学", "per_wang")
        self.svc.return_object("obj_copy", "per_wang", condition="损坏")
        self.assertEqual(self.svc.object_state("obj_copy"), "限制使用")


class PreciousApprovalTest(unittest.TestCase):
    def setUp(self):
        self.svc, _, _ = build_world()

    def test_requires_two_distinct_recorded_approvers(self):
        with self.assertRaises(DomainError):  # 无人批准
            self.svc.borrow_object("obj_old", "教学", "per_wang")
        self.svc.approve_use("obj_old", "per_tan")
        with self.assertRaises(DomainError):  # 仅一人
            self.svc.borrow_object("obj_old", "教学", "per_wang",
                                   approvals=["per_tan"])
        with self.assertRaises(DomainError):  # 同一人冒充两人
            self.svc.borrow_object("obj_old", "教学", "per_wang",
                                   approvals=["per_tan", "per_tan"])
        with self.assertRaises(DomainError):  # 口头批准无正式记录
            self.svc.borrow_object("obj_old", "教学", "per_wang",
                                   approvals=["per_tan", "per_li"])
        self.svc.approve_use("obj_old", "per_li")
        event, _ = self.svc.borrow_object(
            "obj_old", "演出", "per_wang",
            approvals=["per_tan", "per_li"], play_id="", due=TOMORROW)
        self.assertEqual(event["payload"]["批准人"], ["per_tan", "per_li"])
        self.assertEqual(self.svc.object_state("obj_old"), "演出借用")

    def test_approver_must_be_registered_person(self):
        with self.assertRaises(DomainError):
            self.svc.approve_use("obj_old", "外来访客")

    def test_return_restores_and_links_responsibility(self):
        self.svc.approve_use("obj_old", "per_tan")
        self.svc.approve_use("obj_old", "per_li")
        self.svc.borrow_object("obj_old", "教学", "per_wang",
                               approvals=["per_tan", "per_li"])
        self.svc.return_object("obj_old", "per_wang")
        view = self.svc.object_view("obj_old")
        borrow = view["借用记录"][0]
        self.assertEqual(borrow["借出"]["领用人"], "per_wang")
        self.assertEqual(borrow["归还"]["归还人"], "per_wang")
        self.assertEqual(view["当前状态"], "在库")


class IdempotencyTest(unittest.TestCase):
    def test_offline_retry_does_not_double_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "events.json")
            svc = HeritageService(EventStore(path))
            svc.seed_skill_catalog(TECHNIQUES)
            svc.register_person("per_tan", "谭师傅", ["传承人"])
            svc.register_object("道具", "幕布", object_id="obj_cloth")
            first, created1 = svc.record_environment(
                "obj_cloth", 20, 50, "正常", "per_tan", idem="scan-77")
            second, created2 = svc.record_environment(
                "obj_cloth", 99, 99, "异常", "per_tan", idem="scan-77")
            self.assertTrue(created1)
            self.assertFalse(created2)
            self.assertEqual(first["id"], second["id"])
            # 重放内容被忽略，只保存第一次的读数
            self.assertEqual(second["payload"]["温度"], 20)
            first_b, c1 = svc.borrow_object(
                "obj_cloth", "教学", "per_tan", idem="lend-77")
            _second_b, c2 = svc.borrow_object(
                "obj_cloth", "教学", "per_tan", idem="lend-77")
            self.assertTrue(c1)
            self.assertFalse(c2)

            # 模拟设备重启：从同一文件恢复后，迟到重试仍然去重
            restarted = HeritageService(EventStore(path))
            _e, created3 = restarted.borrow_object(
                "obj_cloth", "教学", "per_tan", idem="lend-77")
            self.assertFalse(created3)
            lends = [e for e in restarted.store.events
                     if e["type"] == "借出"]
            self.assertEqual(len(lends), 1)


class StudentGrowthTest(unittest.TestCase):
    def setUp(self):
        self.svc, _, _ = build_world()
        self.svc.register_student(
            "周小满", "2012-06-01", student_id="stu_zhou",
            mentor="per_tan")

    def test_absence_and_mentor_change_remain_in_history(self):
        self.svc.mark_attendance("stu_zhou", "缺课", "操影基础", "per_wang",
                                 note="暴雨停课")
        self.svc.change_mentor("stu_zhou", "per_li", "改修唱腔方向")
        view = self.svc.student_view("stu_zhou")
        self.assertEqual(view["当前师承"], "per_li")
        self.assertEqual([m["至"] for m in view["师承历程"]],
                         ["per_tan", "per_li"])
        self.assertEqual(view["考勤"][0]["状态"], "缺课")

    def test_stage_prerequisites_and_review_results(self):
        # 未通过启蒙不能练
        with self.assertRaises(DomainError):
            self.svc.practice("stu_zhou", "操影启蒙", "obj_copy", "per_tan")
        # 复核中 / 不通过不授予技法
        self.svc.review_work("stu_zhou", "操影启蒙", "复核中", "per_tan")
        self.svc.review_work("stu_zhou", "操影启蒙", "不通过", "per_tan")
        self.assertEqual(self.svc.student_view("stu_zhou")["已通过技法"], [])
        # 失败记录仍然留在历程中
        reviews = self.svc.student_view("stu_zhou")["作品复核"]
        self.assertEqual([r["结论"] for r in reviews], ["复核中", "不通过"])

        self.svc.review_work("stu_zhou", "操影启蒙", "通过", "per_tan",
                             work_ref="work-001")
        # 不能越过前置技法
        with self.assertRaises(DomainError):
            self.svc.review_work("stu_zhou", "影偶修缮", "通过", "per_li")
        self.svc.review_work("stu_zhou", "唱腔入门", "通过", "per_li")
        view = self.svc.student_view("stu_zhou")
        self.assertEqual(view["阶段水平"], "入门")

    def test_practice_scope_and_trace_to_object(self):
        self.svc.review_work("stu_zhou", "操影启蒙", "通过", "per_tan")
        # 复制品可练
        self.svc.practice("stu_zhou", "操影启蒙", "obj_copy", "per_tan")
        # 珍贵原件不得作为练习对象
        with self.assertRaises(DomainError):
            self.svc.practice("stu_zhou", "操影启蒙", "obj_old", "per_tan",
                              replica=False)
        # 普通在库道具在技法通过后可练，并在道具侧留下使用记录
        cloth = self.svc.register_object("道具", "练习幕布",
                                         object_id="obj_cloth")
        self.svc.practice("stu_zhou", "操影启蒙", "obj_cloth", "per_tan",
                          replica=False)
        used = [e for e in self.svc.store.events_for("obj:obj_cloth")
                if e["type"] == "使用记录"]
        self.assertEqual(used[0]["payload"]["学徒"], "stu_zhou")

        view = self.svc.student_view("stu_zhou")
        practices = view["练习记录"]
        self.assertEqual({p["对象"] for p in practices},
                         {"obj_copy", "obj_cloth"})
        self.assertTrue(all(p["指导教师"] == "per_tan" for p in practices))
        self.assertEqual(practices[0]["对象名称"], "关公影偶教学复制品")

    def test_cannot_practice_unpassed_technique(self):
        self.svc.review_work("stu_zhou", "操影启蒙", "通过", "per_tan")
        with self.assertRaises(DomainError):
            self.svc.practice("stu_zhou", "唱腔入门", "obj_copy", "per_li")


class PlayVersionTest(unittest.TestCase):
    def setUp(self):
        self.svc, _, _ = build_world()
        self.svc.register_play("鹤峰断桥新唱", play_id="play_duan",
                               traditional=False)
        self.svc.add_play_version(
            "play_duan", "v1", ["词作者甲"], "per_wang",
            "传统底本整理")
        self.svc.register_object("皮影", "白素贞复制品",
                                 object_id="obj_bai")

    def test_versions_chain_and_creators(self):
        self.svc.add_play_version(
            "play_duan", "v2", ["词作者甲", "唱腔改编乙"], "per_wang",
            "新编水斗唱段", based_on="v1")
        play = self.svc.play_view("play_duan")
        self.assertEqual(play["版本"][1]["依据版本"], "v1")
        self.assertEqual(play["创作者"], ["唱腔改编乙", "词作者甲"])
        with self.assertRaises(DomainError):  # 重复版本号
            self.svc.add_play_version("play_duan", "v1", ["词作者甲"],
                                      "per_wang", "重复")
        with self.assertRaises(DomainError):  # 依据不存在的版本
            self.svc.add_play_version("play_duan", "v9", ["词作者甲"],
                                      "per_wang", "悬空", based_on="v8")

    def test_permission_requires_real_creator(self):
        with self.assertRaises(DomainError):
            self.svc.grant_permission("play_duan", "无关人员", "公开演出")
        with self.assertRaises(DomainError):
            self.svc.grant_permission("play_duan", "词作者甲", "全球巡演")

    def test_public_performance_checks_permission_and_props(self):
        # 未授权：禁止演出
        with self.assertRaises(DomainError):
            self.svc.register_performance(
                "play_duan", "v1", "乡文化礼堂", "per_wang")
        # 过期许可无效
        self.svc.grant_permission("play_duan", "词作者甲", "公开演出",
                                  valid_from=OLD_DATE, valid_to=YESTERDAY)
        with self.assertRaises(DomainError):
            self.svc.register_performance(
                "play_duan", "v1", "乡文化礼堂", "per_wang")
        # 重新授予有效许可
        self.svc.grant_permission("play_duan", "词作者甲", "公开演出",
                                  valid_from=YESTERDAY, valid_to=TOMORROW)
        # 许可齐了，但道具未办演出借用
        with self.assertRaises(DomainError):
            self.svc.register_performance(
                "play_duan", "v1", "乡文化礼堂", "per_wang",
                objects=["obj_bai"])
        self.svc.borrow_object("obj_bai", "演出", "per_wang")
        event, _ = self.svc.register_performance(
            "play_duan", "v1", "乡文化礼堂", "per_wang",
            objects=["obj_bai"])
        self.assertEqual(event["payload"]["使用物件"], ["obj_bai"])

        # 从道具侧能追到参演剧目
        view = self.svc.object_view("obj_bai")
        self.assertIn("鹤峰断桥新唱", view["参与剧目"])
        self.assertEqual(view["演出记录"][0]["场地"], "乡文化礼堂")

    def test_all_version_creators_must_license(self):
        self.svc.add_play_version(
            "play_duan", "v2", ["词作者甲", "唱腔改编乙"], "per_wang",
            "新编水斗唱段", based_on="v1")
        self.svc.grant_permission("play_duan", "词作者甲", "公开演出")
        with self.assertRaises(DomainError) as error:
            self.svc.register_performance(
                "play_duan", "v2", "乡文化礼堂", "per_wang")
        self.assertIn("唱腔改编乙", str(error.exception))


class PersistenceTest(unittest.TestCase):
    def test_reload_keeps_full_history_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "events.json")
            svc = HeritageService(EventStore(path))
            svc.seed_skill_catalog(TECHNIQUES)
            svc.register_person("per_tan", "谭师傅", ["传承人"])
            svc.register_person("per_li", "李师傅", ["传承人"])
            svc.register_object("皮影", "老影偶", precious=True,
                                object_id="obj_x")
            svc.approve_use("obj_x", "per_tan")
            svc.approve_use("obj_x", "per_li")
            svc.borrow_object("obj_x", "教学", "per_tan",
                              approvals=["per_tan", "per_li"])
            svc.return_object("obj_x", "per_tan")
            reloaded = HeritageService(EventStore(path))
            self.assertEqual(reloaded.object_state("obj_x"), "在库")
            self.assertEqual(len(reloaded.object_view("obj_x")["借用记录"]), 1)
            self.assertEqual(reloaded.catalog()[0]["名称"], "操影启蒙")


if __name__ == "__main__":
    unittest.main()
