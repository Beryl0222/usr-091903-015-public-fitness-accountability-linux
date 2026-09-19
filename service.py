"""公共健身资产问效的运行入口。

除保留稳定的 ``/health`` 身份检查外，提供健身指导内容治理的 JSON 接口：
管理端（设施、二维码、版本、三岗审核、发布、暂停与恢复、审计）、
市民扫码只读解析、边缘终端同步与纠正回执、匿名反馈。
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from governance import Backend, GovernanceError

SERVICE_ID = "public-fitness-accountability"
SERVICE_NAME = "公共健身资产问效"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_default_backend():
    store_path = os.environ.get("GOVERNANCE_STORE") or None
    return Backend(store_path=store_path)


class Handler(BaseHTTPRequestHandler):
    """提供健康检查与治理领域接口。"""

    # 测试可注入自己的 Backend（含假时钟）；缺省惰性创建进程级单例。
    backend = None

    def _backend(self):
        if Handler.backend is None:
            Handler.backend = build_default_backend()
        return Handler.backend

    # ------------------------------------------------------------- 基础框架

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise GovernanceError(f"请求体不是合法 JSON：{error}", 400)
        if not isinstance(data, dict):
            raise GovernanceError("请求体必须是 JSON 对象", 400)
        return data

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == "/health":
            self._send_json(200, health_payload())
            return
        try:
            self._route_get(parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()})
        except GovernanceError as error:
            self._send_json(error.status, {"error": str(error)})

    def do_POST(self):
        parts = urlsplit(self.path)
        if parts.path == "/health":
            self._send_json(405, {"error": "健康检查不支持 POST"})
            return
        try:
            body = self._read_json()
            self._route_post(parts.path, body)
        except GovernanceError as error:
            self._send_json(error.status, {"error": str(error)})

    def _route_get(self, path, query):
        backend = self._backend()
        if path == "/qr":
            raise GovernanceError("请使用 /qr/<qr_id>", 404)
        if path.startswith("/qr/"):
            qr_id = path[len("/qr/") :].strip("/")
            self._send_json(200, backend.resolve_qr(qr_id))
            return
        if path == "/admin/releases":
            self._send_json(200, {"releases": backend.list_releases(**self._clean(query, ("qr_id", "version_id", "facility_id")))})
            return
        if path == "/admin/timeline":
            self._send_json(200, backend.timeline(**self._clean(query, ("qr_id", "version_id", "facility_id"))))
            return
        if path.startswith("/admin/notices/") and path.endswith("/delivery"):
            notice_id = path[len("/admin/notices/") : -len("/delivery")]
            self._send_json(200, backend.notice_delivery(notice_id))
            return
        if path == "/admin/feedback":
            self._send_json(200, backend.feedback_summary(**self._clean(query, ("version_id", "qr_id"))))
            return
        self._send_json(404, {"error": f"未知路径 {path}"})

    def _route_post(self, path, body):
        backend = self._backend()
        actor = body.get("actor", "operator")

        if path == "/admin/facilities":
            result = backend.register_facility(body["facility_id"], body["model"], body.get("site", ""), actor)
            self._send_json(201, result)
            return
        if path == "/admin/qrcodes/bind":
            result = backend.bind_qrcode(body["qr_id"], body["facility_id"], actor)
            self._send_json(200, result)
            return
        if path == "/admin/guidance":
            result = backend.create_guidance(body["guide_id"], body["title"], actor)
            self._send_json(201, result)
            return
        if path.startswith("/admin/guidance/") and path.endswith("/versions"):
            guide_id = path[len("/admin/guidance/") : -len("/versions")]
            result = backend.create_version(guide_id, body["content"], actor)
            self._send_json(201, result)
            return
        if path.startswith("/admin/versions/"):
            tail = path[len("/admin/versions/") :]
            version_id, _, action = tail.partition("/")
            if action == "submit":
                self._send_json(200, backend.submit_version(version_id, actor))
                return
            if action == "reviews":
                result = backend.record_review(
                    version_id,
                    body["role"],
                    body["decision"],
                    body.get("reviewer", actor),
                    body.get("comment", ""),
                )
                self._send_json(200, result)
                return
            if action == "withdraw":
                self._send_json(
                    200,
                    backend.withdraw_review(version_id, body["role"], actor, body.get("reason", "")),
                )
                return
        if path == "/admin/releases":
            result = backend.publish(
                body["qr_id"],
                body["version_id"],
                actor,
                body.get("reason", ""),
                idempotency_key=self.headers.get("Idempotency-Key") or body.get("idempotency_key"),
            )
            self._send_json(200, result)
            return
        if path.startswith("/admin/facilities/"):
            tail = path[len("/admin/facilities/") :]
            facility_id, _, action = tail.partition("/")
            if action == "relocate":
                self._send_json(200, backend.relocate_facility(facility_id, body["new_site"], actor, body.get("reason", "")))
                return
            if action == "replace-component":
                self._send_json(
                    200,
                    backend.replace_component(facility_id, body["new_model"], actor, body.get("reason", "")),
                )
                return
        if path == "/admin/risk-events":
            self._send_json(
                200,
                backend.report_risk_event(
                    actor,
                    body.get("reason", ""),
                    qr_ids=body.get("qr_ids"),
                    version_id=body.get("version_id"),
                    facility_id=body.get("facility_id"),
                ),
            )
            return
        if path.startswith("/admin/qrs/") and path.endswith("/reinstate"):
            qr_id = path[len("/admin/qrs/") : -len("/reinstate")]
            self._send_json(200, backend.reinstate_release(qr_id, actor, body.get("reason", "")))
            return

        if path == "/edge/terminals":
            self._send_json(200, backend.register_terminal(body["terminal_id"]))
            return
        if path == "/edge/sync":
            result = backend.edge_sync(
                body["terminal_id"],
                body.get("idempotency_key") or self.headers.get("Idempotency-Key"),
                body.get("cached", []),
            )
            self._send_json(200, result)
            return
        if path == "/edge/ack":
            self._send_json(200, backend.ack_notices(body["terminal_id"], body.get("notice_ids", [])))
            return

        if path == "/feedback/token":
            self._send_json(200, backend.issue_feedback_token())
            return
        if path == "/feedback":
            result = backend.submit_feedback(
                body.get("token"),
                body["qr_id"],
                body.get("version_id"),
                body.get("type"),
                body.get("detail", ""),
                step_index=body.get("step_index"),
            )
            self._send_json(200, result)
            return

        self._send_json(404, {"error": f"未知路径 {path}"})

    @staticmethod
    def _clean(query, keys):
        return {key: query[key] for key in keys if key in query}

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--store", help="治理状态 JSON 快照路径；缺省仅驻留内存")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        backend = Backend(args.store) if args.store else Backend()
        assert backend.get_config()["max_offline_seconds"] > 0
        print("基础检查通过")
        return
    if args.store:
        os.environ["GOVERNANCE_STORE"] = args.store
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
