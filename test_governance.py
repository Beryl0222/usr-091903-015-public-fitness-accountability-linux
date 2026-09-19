"""指导内容治理后端的领域规则测试。"""

import unittest

from governance import (
    DEFAULT_SUBSTITUTE_NOTICE,
    DomainError,
    GovernanceService,
)

NOW = 1_700_000_000.0


def make_service():
    svc = GovernanceService()
    svc.register_facility("F1", model="walker-2000", park="青城公园", location="东区")
    svc.register_facility("F2", model="walker-2000", park="青城公园", location="西区")
    svc.register_facility("F3", model="rider-5", park="敕勒川公园", location="南区")
    svc.bind_qr("QR1", "F1")
    svc.bind_qr("QR2", "F2")
    svc.bind_qr("QR3", "F3")
    svc.create_guidance(
        "G1", title="漫步机使用指导", steps=["握稳扶手", "匀速摆动"],
        applicable_groups=["成人"], contraindications=["髋关节术后"],
        compatible_models=["walker-2000"], now=NOW)
    return svc


def approve_all(svc, guidance_id="G1", version=1, at=NOW):
    for role in ("sports_medicine", "accessibility", "operations"):
        svc.submit_review(guidance_id, version, role, "approve",
                          reviewer=f"{role}-reviewer", now=at)


class PublishGateTest(unittest.TestCase):
    def test_publish_requires_all_three_reviews(self):
        svc = make_service()
        svc.submit_review("G1", 1, "sports_medicine", "approve", "张医生", now=NOW)
        svc.submit_review("G1", 1, "accessibility", "approve", "李复核", now=NOW)
        with self.assertRaises(DomainError):
            svc.publish("G1", 1, now=NOW)
        svc.submit_review("G1", 1, "operations", "approve", "王运营", now=NOW)
        pubs = svc.publish("G1", 1, now=NOW)
        # 只发布到型号兼容的 QR1/QR2，rider-5 的 QR3 不在范围内
        self.assertEqual({p.qr_code for p in pubs}, {"QR1", "QR2"})

    def test_repeated_publish_is_idempotent(self):
        svc = make_service()
        approve_all(svc)
        first = svc.publish("G1", 1, now=NOW)
        again = svc.publish("G1", 1, now=NOW + 10)
        self.assertEqual(len(again), len(first))
        self.assertEqual(
            len([p for p in svc.publications if p.status == "active"]), 2)


class SuspensionTest(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        approve_all(self.svc)
        self.svc.publish("G1", 1, now=NOW)

    def test_facility_event_suspends_only_affected_qr(self):
        suspended = self.svc.facility_event("F1", "part_replaced", now=NOW + 1)
        self.assertEqual([p.qr_code for p in suspended], ["QR1"])
        self.assertEqual(self.svc.current_for_qr("QR1")["status"], "suspended")
        self.assertEqual(self.svc.current_for_qr("QR1")["substitute_notice"],
                         DEFAULT_SUBSTITUTE_NOTICE)
        # QR2 不受影响，仍展示有效内容
        self.assertEqual(self.svc.current_for_qr("QR2")["status"], "effective")

    def test_expert_withdrawal_suspends_published_version(self):
        self.svc.submit_review("G1", 1, "sports_medicine", "withdraw",
                               "张医生", note="动作幅度需重估", now=NOW + 1)
        self.assertEqual(self.svc.current_for_qr("QR1")["status"], "suspended")
        self.assertIn("专家撤回", self.svc.current_for_qr("QR1")["reason"])
        with self.assertRaises(DomainError):
            self.svc.publish("G1", 1, now=NOW + 2)

    def test_history_remains_auditable_after_replacement(self):
        svc = self.svc
        svc.terminal_sync("T1", {"QR1": 1}, now=NOW + 1)
        svc.add_version("G1", title="漫步机指导v2", steps=["握稳扶手", "小幅度摆动"],
                        applicable_groups=["成人", "老人"],
                        contraindications=["髋关节术后"],
                        compatible_models=["walker-2000"], now=NOW + 2)
        approve_all(svc, version=2, at=NOW + 3)
        svc.publish("G1", 2, now=NOW + 4)
        audit = svc.audit_guidance("G1")
        v1, v2 = audit["versions"]
        self.assertEqual(v1["publications"][0]["status"], "superseded")
        self.assertIn("被 v2 替换", v1["publications"][0]["reason"])
        self.assertIsNotNone(v1["publications"][0]["effective_until"])
        # 旧版本的浏览范围（哪些终端缓存过）仍可核对
        qr1_v1 = [p for p in v1["publications"] if p["qr_code"] == "QR1"][0]
        self.assertEqual(qr1_v1["reached_terminals"], ["T1"])
        self.assertEqual(v2["publications"][0]["status"], "active")


class TerminalSyncTest(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        approve_all(self.svc)
        self.svc.publish("G1", 1, now=NOW)

    def test_sync_is_idempotent_and_reports_current(self):
        for _ in range(3):
            result = self.svc.terminal_sync("T1", {"QR1": 1}, now=NOW + 1)
        self.assertEqual(result["current"]["QR1"]["version"], 1)
        self.assertFalse(result["must_reconfirm"])
        # 重复同步不产生新的发布记录
        self.assertEqual(len(self.svc.publications), 2)

    def test_reconnect_requires_confirm_with_current_version(self):
        svc = self.svc
        svc.terminal_sync("T1", {"QR1": 1}, now=NOW + 1)
        svc.mark_offline("T1")
        # 失联期间内容被暂停
        svc.facility_event("F1", "risk_event", now=NOW + 2)
        result = svc.terminal_sync("T1", {"QR1": 1}, now=NOW + 3)
        self.assertTrue(result["must_reconfirm"])
        self.assertEqual(result["current"]["QR1"]["status"], "suspended")
        # 用旧版本确认被拒绝
        with self.assertRaises(DomainError):
            svc.terminal_confirm("T1", {"QR1": 1}, now=NOW + 4)
        # 确认当前有效版本（已暂停则为 None）后放行
        confirmed = svc.terminal_confirm("T1", {"QR1": None}, now=NOW + 5)
        self.assertFalse(confirmed["must_reconfirm"])

    def test_correction_notice_reaches_caching_terminal(self):
        svc = self.svc
        svc.terminal_sync("T1", {"QR1": 1}, now=NOW + 1)
        svc.terminal_sync("T2", {"QR2": 1}, now=NOW + 1)
        svc.facility_event("F1", "migrated", detail="北区", now=NOW + 2)
        notices = svc.audit_notices()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["terminal_id"], "T1")
        self.assertIsNone(notices[0]["delivered_at"])
        # T1 下次同步时送达并确认
        svc.terminal_sync("T1", {"QR1": 1}, now=NOW + 3)
        svc.ack_notice("T1", notices[0]["notice_id"], now=NOW + 4)
        final = svc.audit_notices()[0]
        self.assertIsNotNone(final["delivered_at"])
        self.assertIsNotNone(final["acknowledged_at"])


class FeedbackTest(unittest.TestCase):
    def test_repeat_submissions_do_not_distort_summary(self):
        svc = make_service()
        approve_all(svc)
        svc.publish("G1", 1, now=NOW)
        # 同一人对同一步骤重复提交 20 次，只计一次
        for i in range(20):
            svc.submit_feedback("QR1", step_index=0, category="cannot_complete",
                                reporter_token="anon-a", now=NOW + i)
        svc.submit_feedback("QR1", step_index=0, category="cannot_complete",
                            reporter_token="anon-b", now=NOW)
        svc.submit_feedback("QR1", step_index=0, category="unclear",
                            reporter_token="anon-a", now=NOW)
        summary = svc.feedback_summary("QR1")
        hard = summary["0"]["cannot_complete"]
        self.assertEqual(hard["submitted"], 21)
        self.assertEqual(hard["counted"], 2)
        self.assertEqual(hard["unique_reporters"], 2)
        self.assertEqual(summary["0"]["unclear"]["counted"], 1)


if __name__ == "__main__":
    unittest.main()
