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

    def _change(self, plan, steps):
        plan2 = self.s.make_plan_change("dispatcher", "dispatcher", plan["id"], steps, plan["revision"])
        self.assertEqual("draft", plan2["state"])
        self.assertTrue(plan2["succeeding"])
        plan2 = self.s.submit_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.approve_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        self.assertEqual("pending", plan2["state"], "存在执行中版本时，新版本审批后应进入待接替")
        return plan2

    def test_full_restore_offline_merge_duplicate_and_plan_change(self):
        outage, plan = self.plan()
        report = self.s.field_report("field", "field", plan["id"], 1, "client-1", plan["version"], "completed", "设备已检查")
        self.assertEqual("merged", report["merge_status"])
        confirmed = self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed", "现场照片核验")
        self.assertEqual("confirmed", confirmed["status"])
        protected = self.s.field_report("field", "field", plan["id"], 1, "client-1-protected", plan["version"], "blocked", "补充遥测")
        self.assertEqual("protected", protected["merge_status"])
        # 发起变更后旧版本仍是当前版本，继续接收现场报告；新版本无确认记录
        plan2 = self.s.make_plan_change("dispatcher", "dispatcher", plan["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80, "critical": True},
            {"seq": 2, "action": "送电", "asset": "LINE", "required_mw": 70, "depends_on": [1], "critical": True}], plan["revision"])
        self.assertEqual(0, len(self.s.plan_detail(plan2["id"])["confirmations"]), "待接替前不能带走确认")
        self.assertEqual("active", self.s.plan_detail(plan["id"])["plan"]["state"], "旧版本在接替前继续执行")
        # 旧版本仍可接收新报告并确认
        late = self.s.field_report("field", "field", plan["id"], 2, "client-late", plan["version"], "started", "旧版本现场仍在汇报")
        self.assertEqual("merged", late["merge_status"])
        # 未启用版本不能发布状态
        with self.assertRaises(ApiError):
            self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan2["id"])
        plan2 = self.s.submit_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.approve_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        self.assertEqual("pending", plan2["state"])
        with self.assertRaises(ApiError):
            self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan2["id"])
        plan2 = self.s.activate_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        detail = self.s.plan_detail(plan2["id"])
        self.assertEqual("active", detail["plan"]["state"])
        self.assertEqual(1, len(detail["confirmations"]), "未改动的已确认步骤在启用时带到新版本")
        self.assertEqual("superseded", self.s.plan_detail(plan["id"])["plan"]["state"])
        self.s.field_report("field", "field", plan2["id"], 2, "client-2", plan2["version"], "completed", "已送电")
        self.s.confirm_step("dispatcher", "dispatcher", plan2["id"], 2, "confirmed")
        # 已废止版本不能再发布
        with self.assertRaises(ApiError):
            self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        status = self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan2["id"])
        self.assertEqual("restored", status["status"]["state"])

    def test_changed_confirmations_are_not_carried(self):
        outage, plan = self.plan()
        self.s.field_report("field", "field", plan["id"], 1, "c1", plan["version"], "completed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        # 步骤1要求容量由80改为60（改动过），步骤2保持不变且未确认
        plan2 = self._change(plan, [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 60, "critical": True},
            {"seq": 2, "action": "送电", "asset": "LINE", "required_mw": 70, "depends_on": [1], "critical": True}])
        plan2 = self.s.activate_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        carried = self.s.plan_detail(plan2["id"])["confirmations"]
        self.assertEqual([], carried, "改动过的步骤不能带走确认，需要现场重新报告确认")
        # 步骤1在新版本上未确认，确认步骤2应因依赖未满足被拒绝
        with self.assertRaises(ApiError):
            self.s.confirm_step("dispatcher", "dispatcher", plan2["id"], 2, "confirmed")

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
