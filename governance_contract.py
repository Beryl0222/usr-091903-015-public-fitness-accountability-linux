"""健身指导内容治理后端的契约测试。

每条用例对应需求中的一项承诺：
三岗门禁、定向暂停与替代提示、版本留痕与替换原因、
发布/同步幂等、失联恢复权威清单、纠正通知送达核对、
匿名反馈去重防扭曲、持久化与 HTTP 契约。
"""

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from governance import Backend, GovernanceError, REVIEW_ROLES, ROLE_LABELS
from service import Handler, health_payload


class FakeClock:
    def __init__(self, start=1_700_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def make_content(**overrides):
    content = {
        "title": "太空漫步机 基础动作",
        "steps": ["双手握扶手，双脚站上踏板", "保持身体直立，双腿自然前后摆动", "每组 2 分钟，速度不要过快"],
        "suitable_groups": ["普通成人", "健康老人"],
        "contraindications": ["儿童需陪护", "膝关节术后半年内避免", "眩晕发作期停止"],
    }
    content.update(overrides)
    return content


class BackendTestBase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.backend = Backend(clock=self.clock)
        self.backend.register_facility("F1", "walker-200", "满都海公园 东区")
        self.backend.register_facility("F2", "twister-100", "满都海公园 西区")
        self.backend.bind_qrcode("QR1", "F1")
        self.backend.bind_qrcode("QR2", "F2")
        self.backend.create_guidance("G1", "漫步指导")

    def draft_to_published(self, qr_id="QR1", guide_id="G1", reason="初版发布"):
        version = self.backend.create_version(guide_id, make_content())
        self.backend.submit_version(version["version_id"])
        for role in REVIEW_ROLES:
            self.backend.record_review(version["version_id"], role, "approved", f"reviewer-{role}")
        release = self.backend.publish(qr_id, version["version_id"], "ops", reason)
        return version, release


# --------------------------------------------------------------- 三岗审核门禁


class ReviewGateTests(BackendTestBase):
    def test_publish_requires_all_three_reviews(self):
        version = self.backend.create_version("G1", make_content())
        self.backend.submit_version(version["version_id"])
        with self.assertRaises(GovernanceError) as error:
            self.backend.publish("QR1", version["version_id"], "ops", "提前发布")
        self.assertEqual(error.exception.status, 409)
        body = str(error.exception)
        for label in ROLE_LABELS.values():
            self.assertIn(label, body)

    def test_review_order_is_enforced(self):
        version = self.backend.create_version("G1", make_content())
        self.backend.submit_version(version["version_id"])
        with self.assertRaises(GovernanceError):
            self.backend.record_review(version["version_id"], "operations", "approved", "ops")
        self.backend.record_review(version["version_id"], "sports_medicine", "approved", "doctor")
        with self.assertRaises(GovernanceError):
            self.backend.record_review(version["version_id"], "operations", "approved", "ops")
        self.backend.record_review(version["version_id"], "accessibility", "approved", "a11y")
        self.backend.record_review(version["version_id"], "operations", "approved", "ops")
        self.assertTrue(self.backend.is_publishable(version["version_id"]))

    def test_changes_requested_blocks_later_roles_and_publish(self):
        version = self.backend.create_version("G1", make_content())
        self.backend.submit_version(version["version_id"])
        self.backend.record_review(version["version_id"], "sports_medicine", "changes_requested", "doctor", "老人幅度过大风险")
        with self.assertRaises(GovernanceError):
            self.backend.record_review(version["version_id"], "accessibility", "approved", "a11y")
        with self.assertRaises(GovernanceError):
            self.backend.publish("QR1", version["version_id"], "ops", "强行发布")

    def test_content_must_bind_safety_metadata(self):
        with self.assertRaises(GovernanceError):
            self.backend.create_version("G1", make_content(suitable_groups=[]))
        with self.assertRaises(GovernanceError):
            self.backend.create_version("G1", make_content(contraindications="膝关节术后"))
        with self.assertRaises(GovernanceError):
            self.backend.create_version("G1", make_content(steps=[]))


# ---------------------------------------------------- 迁移 / 换件 / 风险 / 撤回


class SuspensionTests(BackendTestBase):
    def test_relocation_suspends_only_that_facility_with_substitute(self):
        self.draft_to_published("QR1")
        result = self.backend.relocate_facility("F1", "满都海公园 北区", "ops", "步道改造")
        self.assertEqual(result["suspended_qr_ids"], ["QR1"])
        qr1 = self.backend.resolve_qr("QR1")
        # QR2 从未发布 → unavailable；另一个已发布设施不受影响。
        self.draft_to_published("QR2")
        qr2 = self.backend.resolve_qr("QR2")
        self.assertEqual(qr1["status"], "suspended")
        self.assertIn("迁移", qr1["substitute"]["message"])
        self.assertEqual(qr2["status"], "active")

    def test_component_replacement_changes_model_and_suspends_old_guidance(self):
        version, _ = self.draft_to_published("QR1")
        self.backend.replace_component("F1", "walker-300-pro", "ops", "回转部件更换")
        view = self.backend.resolve_qr("QR1")
        self.assertEqual(view["status"], "suspended")
        self.assertIn("walker-300-pro", view["substitute"]["message"])
        self.assertEqual(view["facility"]["model"], "walker-300-pro")
        # 旧版本仍是历史版本，但不再对 QR1 生效。
        self.assertEqual(self.backend.list_releases(qr_id="QR1")[0]["status"], "suspended")
        self.assertEqual(self.backend.list_releases(version_id=version["version_id"])[0]["suspension_event"], "component_replaced")

    def test_risk_event_scoped_to_one_qr_does_not_touch_others(self):
        self.draft_to_published("QR1")
        self.draft_to_published("QR2")
        self.backend.report_risk_event("ops", "儿童独自使用时夹伤报告", qr_ids=["QR1"])
        self.assertEqual(self.backend.resolve_qr("QR1")["status"], "suspended")
        self.assertEqual(self.backend.resolve_qr("QR2")["status"], "active")

    def test_risk_event_scoped_by_version_suspends_all_qrs_carrying_it(self):
        self.backend.bind_qrcode("QR3", "F1")  # 同一设施的第二张贴纸
        version, _ = self.draft_to_published("QR1")
        self.backend.publish("QR3", version["version_id"], "ops", "同版本上墙")
        result = self.backend.report_risk_event("doctor", "动作 2 对术后人群描述不足", version_id=version["version_id"])
        self.assertEqual(set(result["suspended_qr_ids"]), {"QR1", "QR3"})

    def test_expert_withdrawal_after_publish_suspends_and_blocks_republish(self):
        version, _ = self.draft_to_published("QR1")
        outcome = self.backend.withdraw_review(version["version_id"], "sports_medicine", "doctor", "新证据：老人摆动幅度需收紧")
        self.assertEqual(outcome["suspended_qr_ids"], ["QR1"])
        view = self.backend.resolve_qr("QR1")
        self.assertEqual(view["status"], "suspended")
        self.assertIn("撤回", view["suspension_reason"])
        # 撤回后版本不再满足发布条件，重新发布会被拒绝。
        with self.assertRaises(GovernanceError):
            self.backend.publish("QR1", version["version_id"], "ops", "试图再发")

    def test_qr_rebind_suspends_old_content(self):
        self.draft_to_published("QR1")
        self.backend.bind_qrcode("QR1", "F2")
        view = self.backend.resolve_qr("QR1")
        self.assertEqual(view["status"], "suspended")
        self.assertEqual(view["facility"]["facility_id"], "F2")

    def test_reinstate_requires_reason_and_is_blocked_once_new_version_live(self):
        self.draft_to_published("QR1")
        self.backend.relocate_facility("F1", "北区", "ops", "改造")
        with self.assertRaises(GovernanceError):
            self.backend.reinstate_release("QR1", "ops", "")
        v2 = self.backend.create_version("G1", make_content(title="漫步 v2"))
        self.backend.submit_version(v2["version_id"])
        for role in REVIEW_ROLES:
            self.backend.record_review(v2["version_id"], role, "approved", f"r-{role}")
        self.backend.publish("QR1", v2["version_id"], "ops", "迁移复核后新版")
        with self.assertRaises(GovernanceError):
            self.backend.reinstate_release("QR1", "ops", "想恢复旧版")
        self.assertEqual(self.backend.resolve_qr("QR1")["version_id"], v2["version_id"])


# ----------------------------------------------------------- 版本留痕与替换原因


class VersionHistoryTests(BackendTestBase):
    def test_supersede_keeps_history_and_records_why(self):
        v1, r1 = self.draft_to_published("QR1", reason="初版")
        v2 = self.backend.create_version("G1", make_content(title="漫步 v2", steps=["步骤甲", "步骤乙", "步骤丙"]))
        self.backend.submit_version(v2["version_id"])
        for role in REVIEW_ROLES:
            self.backend.record_review(v2["version_id"], role, "approved", f"r-{role}")
        r2 = self.backend.publish("QR1", v2["version_id"], "ops", "安全规范 2026 更新：收紧摆动幅度")
        releases = self.backend.list_releases(qr_id="QR1")
        self.assertEqual([r["status"] for r in releases], ["superseded", "active"])
        self.assertEqual(releases[0]["replaced_by"], r2["release_id"])
        self.assertEqual(releases[0]["replace_reason"], "安全规范 2026 更新：收紧摆动幅度")
        self.assertEqual(releases[0]["version_id"], v1["version_id"])

    def test_timeline_answers_when_effective_and_why_replaced(self):
        self.draft_to_published("QR1", reason="初版")
        v2 = self.backend.create_version("G1", make_content())
        self.backend.submit_version(v2["version_id"])
        for role in REVIEW_ROLES:
            self.backend.record_review(v2["version_id"], role, "approved", f"r-{role}")
        self.backend.publish("QR1", v2["version_id"], "ops", "场地改造后换型")
        actions = [e["action"] for e in self.backend.timeline(qr_id="QR1")["events"]]
        self.assertIn("release.published", actions)
        self.assertIn("release.superseded", actions)
        replace_event = [e for e in self.backend.timeline(qr_id="QR1")["events"] if e["action"] == "release.superseded"][0]
        self.assertEqual(replace_event["reason"], "场地改造后换型")

    def test_repeated_publish_same_version_does_not_create_second_release(self):
        version, release = self.draft_to_published("QR1")
        again = self.backend.publish("QR1", version["version_id"], "ops", "重复点击")
        self.assertEqual(again["release_id"], release["release_id"])
        self.assertEqual(len(self.backend.list_releases(qr_id="QR1")), 1)


# ------------------------------------------------------- 边缘同步幂等与失联恢复


class EdgeSyncTests(BackendTestBase):
    def test_sync_is_idempotent_and_never_publishes(self):
        version, release = self.draft_to_published("QR1")
        before = len(self.backend.list_releases())
        payload = {
            "terminal_id": "KIOSK-7",
            "idempotency_key": "sync-0001",
            "cached": [{"qr_id": "QR1", "version_id": version["version_id"]}],
        }
        first = self.backend.edge_sync(**payload)
        second = self.backend.edge_sync(**payload)
        third = self.backend.edge_sync(**{**payload, "cached": [{"qr_id": "QR1", "version_id": version["version_id"]}]})
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        sync_events = [e for e in self.backend.timeline()["events"] if e["action"] == "edge.synced"]
        self.assertEqual(len(sync_events), 1)
        self.assertEqual(len(self.backend.list_releases()), before)

    def test_reconnect_confirms_current_effective_version_and_corrects_stale_cache(self):
        version, _ = self.draft_to_published("QR1")
        # 终端失联期间：设施换型，旧内容被暂停。
        sync = self.backend.edge_sync(
            "KIOSK-7", "sync-a", [{"qr_id": "QR1", "version_id": version["version_id"]}]
        )
        self.assertEqual(sync["effective"][0]["state"], "active")
        self.backend.replace_component("F1", "walker-300-pro", "ops", "换型")
        recovered = self.backend.edge_sync(
            "KIOSK-7", "sync-b", [{"qr_id": "QR1", "version_id": version["version_id"]}]
        )
        self.assertTrue(recovered["authoritative"])
        self.assertEqual(recovered["effective"][0]["state"], "suspended")
        self.assertIn("walker-300-pro", recovered["effective"][0]["substitute"]["message"])
        self.assertEqual(len(recovered["corrections"]), 1)
        self.assertEqual(recovered["corrections"][0]["kind"], "suspended")

    def test_correction_delivery_can_be_verified_until_terminal_acks(self):
        version, _ = self.draft_to_published("QR1")
        self.backend.edge_sync("KIOSK-7", "s1", [{"qr_id": "QR1", "version_id": version["version_id"]}])
        self.backend.edge_sync("KIOSK-9", "s1", [{"qr_id": "QR1", "version_id": version["version_id"]}])
        self.backend.report_risk_event("ops", "风险", qr_ids=["QR1"])
        # 从未缓存旧内容的终端不应收到纠正通知。
        self.backend.edge_sync("KIOSK-NEW", "s1", [])
        k7 = self.backend.edge_sync("KIOSK-7", "s2", [{"qr_id": "QR1", "version_id": version["version_id"]}])
        notice_id = k7["corrections"][0]["notice_id"]
        delivery = self.backend.notice_delivery(notice_id)
        self.assertEqual(delivery["summary"], {"total": 2, "pending": 1, "delivered": 1, "acked": 0})
        states = {t["terminal_id"]: t["state"] for t in delivery["terminals"]}
        self.assertEqual(states, {"KIOSK-7": "delivered", "KIOSK-9": "pending"})
        # 终端撤下旧缓存后回执；未送达的通知不能回执。
        self.backend.ack_notices("KIOSK-7", [notice_id])
        with self.assertRaises(GovernanceError):
            self.backend.ack_notices("KIOSK-9", [notice_id])
        delivery = self.backend.notice_delivery(notice_id)
        self.assertEqual(delivery["summary"], {"total": 2, "pending": 1, "delivered": 0, "acked": 1})

    def test_supersede_notice_reaches_terminal_that_cached_old_version(self):
        v1, _ = self.draft_to_published("QR1")
        self.backend.edge_sync("K", "s1", [{"qr_id": "QR1", "version_id": v1["version_id"]}])
        v2 = self.backend.create_version("G1", make_content(title="v2"))
        self.backend.submit_version(v2["version_id"])
        for role in REVIEW_ROLES:
            self.backend.record_review(v2["version_id"], role, "approved", "r")
        self.backend.publish("QR1", v2["version_id"], "ops", "规范更新")
        sync = self.backend.edge_sync("K", "s2", [{"qr_id": "QR1", "version_id": v1["version_id"]}])
        self.assertEqual(sync["effective"][0]["version_id"], v2["version_id"])
        self.assertEqual(sync["corrections"][0]["kind"], "superseded")
        self.backend.ack_notices("K", [sync["corrections"][0]["notice_id"]])
        # 已回执后再次同步不再重复下发同一通知。
        again = self.backend.edge_sync("K", "s3", [{"qr_id": "QR1", "version_id": v2["version_id"]}])
        self.assertEqual(again["corrections"], [])


# --------------------------------------------------------------- 匿名反馈去重


class FeedbackTests(BackendTestBase):
    def _send(self, qr="QR1", version=None, ftype="cannot_complete", detail="第二步腿抬不起来", step=1):
        token = self.backend.issue_feedback_token()["token"]
        return self.backend.submit_feedback(token, qr, version, ftype, detail, step_index=step)

    def test_duplicate_submissions_from_same_person_collapse(self):
        version, _ = self.draft_to_published("QR1")
        first = self._send(version=version["version_id"])
        self.assertTrue(first["accepted"])
        token = self.backend.issue_feedback_token()["token"]
        # 同一令牌、空白/标点差异视为同一条。
        merged = self.backend.submit_feedback(
            token, "QR1", version["version_id"], "cannot_complete", "第二步 腿抬不起来！", step_index=1
        )
        # 注意：这是新令牌的跨令牌指纹折叠（1 小时内），归入既有条目而非新增。
        self.assertFalse(merged["accepted"])
        self.assertEqual(merged["merged_into"], first["feedback_id"])
        # 同一令牌直接重复提交被拒绝。
        with self.assertRaises(GovernanceError):
            self.backend.submit_feedback(token, "QR1", version["version_id"], "cannot_complete", "第二步腿抬不起来", step_index=1)
        summary = self.backend.feedback_summary(version_id=version["version_id"])
        self.assertEqual(summary["unique_submitters"], 1)
        self.assertEqual(summary["unique_submissions"], 1)
        self.assertEqual(summary["duplicates_collapsed"], 1)

    def test_different_people_count_separately(self):
        version, _ = self.draft_to_published("QR1")
        self._send(version=version["version_id"])
        t2 = self.backend.issue_feedback_token()["token"]
        self.backend.submit_feedback(t2, "QR1", version["version_id"], "unclear", "第三步专业词看不懂", step_index=2)
        t3 = self.backend.issue_feedback_token()["token"]
        self.backend.submit_feedback(t3, "QR1", version["version_id"], "cannot_complete", "扶手够不到", step_index=0)
        summary = self.backend.feedback_summary(version_id=version["version_id"])
        self.assertEqual(summary["unique_submitters"], 3)
        self.assertEqual(summary["by_type"], {"cannot_complete": 2, "unclear": 1})
        self.assertEqual(summary["by_step"], {"1": 1, "2": 1, "0": 1})

    def test_feedback_requires_token(self):
        version, _ = self.draft_to_published("QR1")
        with self.assertRaises(GovernanceError) as error:
            self.backend.submit_feedback("nope", "QR1", version["version_id"], "unclear", "x")
        self.assertEqual(error.exception.status, 403)


# ------------------------------------------------------------------- 持久化


class PersistenceTests(unittest.TestCase):
    def test_state_survives_restart_via_snapshot_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            backend = Backend(store_path=path, clock=FakeClock())
            backend.register_facility("F1", "m1", "site-a")
            backend.bind_qrcode("QR1", "F1")
            backend.create_guidance("G1", "g")
            version = backend.create_version("G1", make_content())
            backend.submit_version(version["version_id"])
            for role in REVIEW_ROLES:
                backend.record_review(version["version_id"], role, "approved", "r")
            backend.publish("QR1", version["version_id"], "ops", "初版")

            restored = Backend(store_path=path, clock=FakeClock())
            view = restored.resolve_qr("QR1")
            self.assertEqual(view["version_id"], version["version_id"])
            self.assertEqual(view["facility"]["model"], "m1")
            self.assertEqual(len(restored.list_releases()), 1)


# ----------------------------------------------------------------- HTTP 契约


class HttpContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clock = FakeClock()
        Handler.backend = Backend(clock=cls.clock)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        Handler.backend = None

    def _request(self, method, path, body=None, headers=None, expect_error=False):
        data = None
        hdrs = {"Content-Type": "application/json; charset=utf-8"}
        if headers:
            hdrs.update(headers)
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(f"{self.base_url}{path}", data=data, headers=hdrs, method=method)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if not expect_error:
                raise
            return error.code, json.loads(error.read().decode("utf-8"))

    def _publish_full(self, qr_id="QRW", facility_id="FW"):
        guide_id = f"GW-{qr_id}"
        self._request("POST", "/admin/facilities", {"facility_id": facility_id, "model": "m", "site": "site"})
        self._request("POST", "/admin/qrcodes/bind", {"qr_id": qr_id, "facility_id": facility_id})
        self._request("POST", "/admin/guidance", {"guide_id": guide_id, "title": "g"})
        status, version = self._request(
            "POST", f"/admin/guidance/{guide_id}/versions", {"content": make_content()}
        )
        vid = version["version_id"]
        self._request("POST", f"/admin/versions/{vid}/submit", {})
        for role in REVIEW_ROLES:
            self._request("POST", f"/admin/versions/{vid}/reviews", {"role": role, "decision": "approved"})
        status, release = self._request(
            "POST", "/admin/releases", {"qr_id": qr_id, "version_id": vid, "reason": "http 初版"}
        )
        return vid, release["release_id"]

    def test_health_contract_unchanged(self):
        status, body = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, health_payload())

    def test_unknown_route_remains_404(self):
        status, _ = self._request("GET", "/unknown", expect_error=True)
        self.assertEqual(status, 404)

    def test_full_publish_flow_and_qr_resolution(self):
        vid, _ = self._publish_full()
        status, view = self._request("GET", "/qr/QRW")
        self.assertEqual(status, 200)
        self.assertEqual(view["status"], "active")
        self.assertEqual(view["version_id"], vid)
        self.assertIn("cache_policy", view)
        self.assertTrue(view["cache_policy"]["must_reconcile_on_reconnect"])

    def test_publish_gate_enforced_over_http(self):
        self._request("POST", "/admin/facilities", {"facility_id": "FG", "model": "m", "site": "s"})
        self._request("POST", "/admin/qrcodes/bind", {"qr_id": "QRG", "facility_id": "FG"})
        self._request("POST", "/admin/guidance", {"guide_id": "GG", "title": "g"})
        _, version = self._request("POST", "/admin/guidance/GG/versions", {"content": make_content()})
        self._request("POST", f"/admin/versions/{version['version_id']}/submit", {})
        status, body = self._request(
            "POST",
            "/admin/releases",
            {"qr_id": "QRG", "version_id": version["version_id"], "reason": "抢跑"},
            expect_error=True,
        )
        self.assertEqual(status, 409)
        self.assertIn("运动医学审核", body["error"])

    def test_publish_idempotency_key_over_http(self):
        vid, release_id = self._publish_full(qr_id="QRI", facility_id="FI")
        headers = {"Idempotency-Key": "cart-77"}
        body = {"qr_id": "QRI", "version_id": vid, "reason": "边缘重试"}
        _, one = self._request("POST", "/admin/releases", body, headers=headers)
        _, two = self._request("POST", "/admin/releases", body, headers=headers)
        self.assertEqual(one["release_id"], two["release_id"])
        self.assertEqual(one["release_id"], release_id)

    def test_edge_sync_and_correction_ack_flow_over_http(self):
        vid, _ = self._publish_full(qr_id="QRE", facility_id="FE")
        self._request("POST", "/edge/terminals", {"terminal_id": "T1"})
        sync_body = {
            "terminal_id": "T1",
            "idempotency_key": "k1",
            "cached": [{"qr_id": "QRE", "version_id": vid}],
        }
        _, sync1 = self._request("POST", "/edge/sync", sync_body)
        _, sync2 = self._request("POST", "/edge/sync", sync_body)
        self.assertEqual(sync1, sync2)
        self._request("POST", "/admin/risk-events", {"reason": "http 风险", "qr_ids": ["QRE"]})
        _, recovered = self._request(
            "POST",
            "/edge/sync",
            {"terminal_id": "T1", "idempotency_key": "k2", "cached": [{"qr_id": "QRE", "version_id": vid}]},
        )
        self.assertEqual(recovered["effective"][0]["state"], "suspended")
        notice_id = recovered["corrections"][0]["notice_id"]
        self._request("POST", "/edge/ack", {"terminal_id": "T1", "notice_ids": [notice_id]})
        _, delivery = self._request("GET", f"/admin/notices/{notice_id}/delivery")
        self.assertEqual(delivery["summary"]["acked"], 1)

    def test_feedback_token_and_dedup_over_http(self):
        vid, _ = self._publish_full(qr_id="QRF", facility_id="FF")
        _, token_body = self._request("POST", "/feedback/token", {})
        payload = {"token": token_body["token"], "qr_id": "QRF", "version_id": vid, "type": "unclear", "detail": "第二步看不懂", "step_index": 1}
        status, accepted = self._request("POST", "/feedback", payload)
        self.assertTrue(accepted["accepted"])
        status, duplicated = self._request("POST", "/feedback", payload, expect_error=True)
        self.assertEqual(status, 409)
        _, summary = self._request("GET", f"/admin/feedback?version_id={vid}")
        self.assertEqual(summary["unique_submitters"], 1)


if __name__ == "__main__":
    unittest.main()
