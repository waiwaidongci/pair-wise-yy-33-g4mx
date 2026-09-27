import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, GridService, Store


class GridFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = GridService(Store(Path(self.tmp.name) / "g.db"))
        self.sub = self.s.register_asset("dispatcher", "dispatcher", "SUB", "中心站", "substation", 200, "A")
        self.line = self.s.register_asset("dispatcher", "dispatcher", "LINE", "线路", "line", 100, "A", self.sub["id"])
        self.s.register_facility("dispatcher", "dispatcher", "医院", "hospital", self.sub["id"], 1, 50)

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def plan(self, code="OUT-1"):
        outage = self.s.create_outage("dispatcher", "dispatcher", code, "线路跳闸", ["A"])
        plan = self.s.create_plan("dispatcher", "dispatcher", outage["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80, "critical": True},
            {"seq": 2, "action": "送电", "asset": "LINE", "required_mw": 70, "depends_on": [1], "critical": True}])
        plan = self.s.submit_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.approve_plan("dispatcher", "dispatcher", plan["id"], plan["revision"], "安全校核通过")
        return outage, self.s.activate_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])

    def test_full_restore_offline_merge_duplicate_and_plan_change(self):
        outage, plan = self.plan()
        report = self.s.field_report("field", "field", plan["id"], 1, "client-1", plan["version"], "completed", "设备已检查")
        self.assertEqual("merged", report["merge_status"])
        confirmed = self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed", "现场照片核验")
        self.assertEqual("confirmed", confirmed["status"])
        protected = self.s.field_report("field", "field", plan["id"], 1, "client-1-protected", plan["version"], "blocked", "补充遥测")
        self.assertEqual("protected", protected["merge_status"])
        plan2 = self.s.make_plan_change("dispatcher", "dispatcher", plan["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80, "critical": True},
            {"seq": 2, "action": "送电", "asset": "LINE", "required_mw": 70, "depends_on": [1], "critical": True}], plan["revision"])
        # 变更只创建待接替草稿：旧版本仍为当前，确认不提前带走
        self.assertEqual("pending_succession", self.s.plan_detail(plan2["id"])["plan"]["lifecycle"])
        self.assertEqual(0, len(self.s.plan_detail(plan2["id"])["confirmations"]))
        self.assertEqual("active", self.s.plan_detail(plan["id"])["plan"]["state"])
        plan2 = self.s.submit_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.approve_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.activate_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        # 启用时一次切换：未改动步骤的确认带到新版本
        self.assertEqual(1, len(self.s.plan_detail(plan2["id"])["confirmations"]))
        self.assertEqual("superseded", self.s.plan_detail(plan["id"])["plan"]["state"])
        self.s.field_report("field", "field", plan2["id"], 2, "client-2", plan2["version"], "completed", "已送电")
        self.s.confirm_step("dispatcher", "dispatcher", plan2["id"], 2, "confirmed")
        status = self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan2["id"])
        self.assertEqual("restored", status["status"]["state"])
        # 废止版本不能再发布状态
        with self.assertRaises(ApiError):
            self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])

    def test_pending_succession_keeps_old_plan_live_and_changed_steps_not_carried(self):
        outage, plan = self.plan("OUT-3")
        self.s.field_report("field", "field", plan["id"], 1, "c-1", plan["version"], "completed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        # 变更改动步骤2（容量变化），并新增步骤3
        plan2 = self.s.make_plan_change("dispatcher", "dispatcher", plan["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80, "critical": True},
            {"seq": 2, "action": "送电", "asset": "LINE", "required_mw": 90, "depends_on": [1], "critical": True},
            {"seq": 3, "action": "复核", "asset": "SUB", "required_mw": 20, "depends_on": [2]}], plan["revision"])
        self.assertEqual("pending_succession", self.s.plan_detail(plan2["id"])["plan"]["lifecycle"])
        # 待接替期间旧版本继续接收现场报告并允许确认步骤
        mid = self.s.field_report("field", "field", plan["id"], 2, "c-mid", plan["version"], "completed", "接替前完成")
        self.assertEqual("merged", mid["merge_status"])
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")
        # 待接替草稿既不能接现场报告也不能发布状态
        not_live = self.s.field_report("field", "field", plan2["id"], 1, "c-draft", plan2["version"], "completed")
        self.assertEqual("conflict", not_live["merge_status"])
        with self.assertRaises(ApiError):
            self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan2["id"])
        # 同一执行中计划只能有一个待接替版本
        with self.assertRaises(ApiError):
            self.s.make_plan_change("dispatcher", "dispatcher", plan["id"], [
                {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80}], plan["revision"])
        plan2 = self.s.submit_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.approve_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.activate_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        self.assertEqual("current", self.s.plan_detail(plan2["id"])["plan"]["lifecycle"])
        self.assertEqual("superseded", self.s.plan_detail(plan["id"])["plan"]["lifecycle"])
        carried = {row["step_no"] for row in self.s.plan_detail(plan2["id"])["confirmations"]}
        # 步骤1未改动→带走；步骤2被改动→不带走；步骤3新增→无确认
        self.assertEqual({1}, carried)
        # 切换后旧版本的新现场报告只能记为冲突（已废止）
        stale = self.s.field_report("field", "field", plan["id"], 1, "c-old", plan["version"], "completed")
        self.assertEqual("conflict", stale["merge_status"])
        self.assertIn("废止", stale["conflict_reason"])
        # 只有当前启用版本可以发布
        status = self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan2["id"])
        self.assertEqual("restoring", status["status"]["state"])

    def test_anomaly_stale_report_dependency_and_permissions(self):
        outage, plan = self.plan("OUT-2")
        anomaly = self.s.record_telemetry("operator", "operator", self.line["id"], 500, 220, "2026-09-24T00:00:00Z")
        self.assertFalse(anomaly["valid"])
        with self.assertRaises(ApiError):
            self.s.field_report("operator", "operator", plan["id"], 1, "bad-role", plan["version"], "completed")
        stale = self.s.field_report("field", "field", plan["id"], 2, "stale", plan["version"] - 1, "completed")
        self.assertEqual("conflict", stale["merge_status"])
        with self.assertRaises(ApiError):
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")
        with self.assertRaises(ApiError):
            self.s.create_plan("dispatcher", "dispatcher", outage["id"], [{"seq": 1, "action": "送电", "asset": "LINE", "required_mw": 101}])


if __name__ == "__main__": unittest.main()
