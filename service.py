"""公共健身资产问效的基础运行入口。

除健康检查外，承载扫码健身指导内容治理后端的 JSON 接口：
设施与二维码绑定、三级审核发布、局部暂停与替代提示、
边缘终端同步确认、匿名反馈与审计。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from governance import DomainError, GovernanceService

SERVICE_ID = "public-fitness-accountability"
SERVICE_NAME = "公共健身资产问效"

SERVICE = GovernanceService()


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def _dataclass_view(obj):
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _dataclass_view(v) for k, v in vars(obj).items()}
    if isinstance(obj, list):
        return [_dataclass_view(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _dataclass_view(v) for k, v in obj.items()}
    return obj


class Handler(BaseHTTPRequestHandler):
    """提供健康检查与指导内容治理接口。"""

    def do_GET(self):
        path, query = self._split()
        parts = [p for p in path.split("/") if p]
        try:
            if path == "/health":
                return self._json(200, health_payload())
            if len(parts) == 3 and parts[0] == "guidance" and parts[2] == "audit":
                return self._json(200, SERVICE.audit_guidance(parts[1]))
            if len(parts) == 3 and parts[0] == "qrcodes" and parts[2] == "current":
                return self._json(200, SERVICE.current_for_qr(parts[1]))
            if parts == ["feedback", "summary"]:
                qr_code = parse_qs(query).get("qr_code", [""])[0]
                if not qr_code:
                    raise DomainError("缺少 qr_code 参数")
                return self._json(200, SERVICE.feedback_summary(qr_code))
            if parts == ["notices", "audit"]:
                return self._json(200, SERVICE.audit_notices())
            self.send_error(404)
        except DomainError as error:
            self._json(400, {"error": str(error)})

    def do_POST(self):
        path, _query = self._split()
        parts = [p for p in path.split("/") if p]
        try:
            body = self._body()
            if parts == ["facilities"]:
                facility = SERVICE.register_facility(
                    body["facility_id"], body["model"], body["park"], body["location"])
                return self._json(201, _dataclass_view(facility))
            if len(parts) == 3 and parts[0] == "facilities" and parts[2] == "events":
                suspended = SERVICE.facility_event(
                    parts[1], body["kind"], body.get("detail", ""),
                    body.get("substitute"))
                return self._json(200, {"suspended": _dataclass_view(suspended)})
            if parts == ["qrcodes"]:
                return self._json(201, SERVICE.bind_qr(body["qr_code"], body["facility_id"]))
            if parts == ["guidance"]:
                version = SERVICE.create_guidance(
                    body["guidance_id"], body["title"], body["steps"],
                    body.get("applicable_groups", []), body.get("contraindications", []),
                    body.get("compatible_models", []))
                return self._json(201, _dataclass_view(version))
            if len(parts) == 3 and parts[0] == "guidance" and parts[2] == "versions":
                version = SERVICE.add_version(
                    parts[1], body["title"], body["steps"],
                    body.get("applicable_groups", []), body.get("contraindications", []),
                    body.get("compatible_models", []))
                return self._json(201, _dataclass_view(version))
            if len(parts) == 5 and parts[0] == "guidance" and parts[2] == "versions" \
                    and parts[4] == "reviews":
                review = SERVICE.submit_review(
                    parts[1], int(parts[3]), body["role"], body["decision"],
                    body["reviewer"], body.get("note", ""))
                return self._json(200, _dataclass_view(review))
            if len(parts) == 5 and parts[0] == "guidance" and parts[2] == "versions" \
                    and parts[4] == "publish":
                pubs = SERVICE.publish(parts[1], int(parts[3]))
                return self._json(200, {"published": _dataclass_view(pubs)})
            if len(parts) == 3 and parts[0] == "terminals" and parts[2] == "sync":
                return self._json(200, SERVICE.terminal_sync(
                    parts[1], body.get("known", {})))
            if len(parts) == 3 and parts[0] == "terminals" and parts[2] == "confirm":
                return self._json(200, SERVICE.terminal_confirm(
                    parts[1], body.get("accepted", {})))
            if len(parts) == 3 and parts[0] == "terminals" and parts[2] == "offline":
                return self._json(200, _dataclass_view(SERVICE.mark_offline(parts[1])))
            if len(parts) == 4 and parts[0] == "terminals" and parts[2] == "notices" \
                    and parts[3] == "ack":
                notice = SERVICE.ack_notice(parts[1], body["notice_id"])
                return self._json(200, SERVICE._notice_view(notice))
            if parts == ["feedback"]:
                entry = SERVICE.submit_feedback(
                    body["qr_code"], int(body["step_index"]), body["category"],
                    body["reporter_token"], body.get("guidance_version"))
                return self._json(201, _dataclass_view(entry))
            self.send_error(404)
        except DomainError as error:
            self._json(400, {"error": str(error)})
        except (KeyError, TypeError, ValueError) as error:
            self._json(400, {"error": f"请求参数不完整或格式错误: {error}"})

    def _split(self):
        parsed = urlparse(self.path)
        return parsed.path, parsed.query

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
