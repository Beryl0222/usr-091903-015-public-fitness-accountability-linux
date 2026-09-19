"""健身指导内容治理后端的领域逻辑。

设计目标（与业务约定一一对应）：

* 每个二维码与设施型号、安装位置、适用人群、禁忌条件、动作版本绑定；
* 运动医学审核、无障碍复核、运营批准三岗全部完成后才允许发布，且有先后顺序；
* 设施迁移、部件更换、风险事件、专家撤回只暂停受影响的发布，并生成替代提示；
* 版本不可变，历史发布与其浏览（生效）范围长期可核对；
* 发布是服务端状态迁移，边缘终端重复同步不会产生多次发布，同步按幂等键去重；
* 终端失联恢复后，同步响应首先给出当前有效版本的权威清单；
* 旧版本被暂停或替换时生成纠正通知，可核对曾缓存旧内容的终端是否送达、是否应用；
* 市民匿名反馈按匿名令牌与内容指纹去重，统计只计不同提交人，避免少量重复提交扭曲结论；
* 全部状态迁移写入审计时间线，说明何时、对哪些设施生效、为何被替换。

仅依赖 Python 标准库，持久化为 JSON 文件的原子快照。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

REVIEW_ROLES = ("sports_medicine", "accessibility", "operations")
ROLE_LABELS = {
    "sports_medicine": "运动医学审核",
    "accessibility": "无障碍复核",
    "operations": "运营批准",
}
ROLE_ORDER = {role: index for index, role in enumerate(REVIEW_ROLES)}

FEEDBACK_TYPES = ("unclear", "cannot_complete")
EVENT_SUSPENSION = {"relocation", "component_replaced", "risk_event", "expert_withdrawal"}

DEFAULT_MAX_OFFLINE_SECONDS = 24 * 60 * 60


class GovernanceError(Exception):
    """业务规则冲突，``status`` 是对应的 HTTP 状态码。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _new_token():
    return uuid.uuid4().hex


def _normalize_detail(detail):
    """折叠空白与标点差异，使同一市民的重复点击落到同一指纹。"""
    if detail is None:
        return ""
    # Python 3 的 \\w 含中文与字母数字；剥离标点（含中文标点）和空白后小写化。
    return "".join(re.findall(r"\w+", str(detail), flags=re.UNICODE)).lower()[:200]


class Backend:
    """线程安全的治理后端；``store_path`` 为 None 时仅驻留内存。"""

    def __init__(self, store_path=None, clock=time.time, max_offline_seconds=DEFAULT_MAX_OFFLINE_SECONDS):
        self.store_path = store_path
        self.clock = clock
        self._lock = threading.RLock()
        self.max_offline_seconds = max_offline_seconds
        self._state = self._empty_state()
        if store_path and os.path.exists(store_path):
            with open(store_path, "r", encoding="utf-8") as handle:
                self._state = json.load(handle)

    @staticmethod
    def _empty_state():
        return {
            "config": {"max_offline_seconds": DEFAULT_MAX_OFFLINE_SECONDS},
            "seq": {},
            "facilities": {},
            "qrcodes": {},
            "guidance": {},
            "versions": {},
            "releases": [],
            "suspensions": [],
            "notices": [],
            "events": [],
            "terminals": {},
            "feedback_tokens": {},
            "feedback": [],
            "idempotency": {},
        }

    # ------------------------------------------------------------------ 持久化

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def save(self):
        if not self.store_path:
            return
        directory = os.path.dirname(os.path.abspath(self.store_path))
        os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self.store_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(self._state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_path, self.store_path)

    def _id(self, kind):
        seq = self._state["seq"]
        seq[kind] = seq.get(kind, 0) + 1
        return f"{kind}_{seq[kind]:06d}"

    def _event(self, action, actor, target, reason=None, **details):
        event = {
            "event_id": self._id("evt"),
            "at": _iso(self.clock()),
            "action": action,
            "actor": actor,
            "target": target,
            "reason": reason,
            "details": details,
        }
        self._state["events"].append(event)
        return event

    # ------------------------------------------------------------ 设施与二维码

    def register_facility(self, facility_id, model, site, actor="operator"):
        """登记设施（器材型号、安装位置）。"""
        if not facility_id or not model:
            raise GovernanceError("facility_id 与 model 不能为空")
        with self._lock:
            if facility_id in self._state["facilities"]:
                raise GovernanceError(f"设施 {facility_id} 已存在", 409)
            facility = {
                "facility_id": facility_id,
                "model": model,
                "site": site or "",
                "state": "active",
                "registered_at": _iso(self.clock()),
            }
            self._state["facilities"][facility_id] = facility
            self._event("facility.registered", actor, f"facility:{facility_id}", model=model, site=site)
            self.save()
            return copy.deepcopy(facility)

    def bind_qrcode(self, qr_id, facility_id, actor="operator"):
        """把二维码绑定到具体设施；一个二维码同一时刻只指向一台设施。"""
        with self._lock:
            if facility_id not in self._state["facilities"]:
                raise GovernanceError(f"设施 {facility_id} 不存在", 404)
            existing = self._state["qrcodes"].get(qr_id)
            qr = {
                "qr_id": qr_id,
                "facility_id": facility_id,
                "bound_at": _iso(self.clock()),
            }
            self._state["qrcodes"][qr_id] = qr
            if existing and existing["facility_id"] != facility_id:
                # 贴纸改绑到别的设施：旧动作不再适用，先暂停再等重新发布。
                self._suspend_locked(
                    [qr_id],
                    "qr_rebound",
                    f"二维码由设施 {existing['facility_id']} 改绑至 {facility_id}",
                    actor,
                    {"kind": "rebind", "message": "该二维码已改绑到其他器材，原动作指导暂停，正在重新匹配。"},
                )
            self._event(
                "qrcode.bound",
                actor,
                f"qr:{qr_id}",
                facility_id=facility_id,
                previous_facility_id=existing["facility_id"] if existing else None,
            )
            self.save()
            return copy.deepcopy(qr)

    def _require_qr(self, qr_id):
        qr = self._state["qrcodes"].get(qr_id)
        if not qr:
            raise GovernanceError(f"二维码 {qr_id} 不存在", 404)
        return qr

    # --------------------------------------------------------------- 指导与版本

    def create_guidance(self, guide_id, title, actor="editor"):
        with self._lock:
            if guide_id in self._state["guidance"]:
                raise GovernanceError(f"指导 {guide_id} 已存在", 409)
            guide = {"guide_id": guide_id, "title": title, "created_at": _iso(self.clock())}
            self._state["guidance"][guide_id] = guide
            self._event("guidance.created", actor, f"guide:{guide_id}", title=title)
            self.save()
            return copy.deepcopy(guide)

    def create_version(self, guide_id, content, actor="editor"):
        """为指导创建一个不可变新版本（适用人群、禁忌、动作步骤一并冻结）。"""
        self._validate_content(content)
        with self._lock:
            if guide_id not in self._state["guidance"]:
                raise GovernanceError(f"指导 {guide_id} 不存在", 404)
            revisions = [
                v["revision"]
                for v in self._state["versions"].values()
                if v["guide_id"] == guide_id
            ]
            revision = (max(revisions) + 1) if revisions else 1
            version_id = f"{guide_id}:v{revision}"
            version = {
                "version_id": version_id,
                "guide_id": guide_id,
                "revision": revision,
                "content": copy.deepcopy(content),
                "created_by": actor,
                "created_at": _iso(self.clock()),
                "status": "draft",
                "reviews": {},
            }
            self._state["versions"][version_id] = version
            self._event("version.created", actor, f"version:{version_id}", revision=revision)
            self.save()
            return copy.deepcopy(version)

    @staticmethod
    def _validate_content(content):
        if not isinstance(content, dict):
            raise GovernanceError("content 必须是对象")
        if not content.get("title"):
            raise GovernanceError("content.title 不能为空")
        steps = content.get("steps")
        if not isinstance(steps, list) or not steps or not all(isinstance(s, str) and s.strip() for s in steps):
            raise GovernanceError("content.steps 必须是非空字符串数组")
        groups = content.get("suitable_groups")
        if not isinstance(groups, list) or not groups:
            raise GovernanceError("content.suitable_groups 必须注明适用人群")
        contra = content.get("contraindications")
        if not isinstance(contra, list):
            raise GovernanceError("content.contraindications 必须是数组")

    def _require_version(self, version_id):
        version = self._state["versions"].get(version_id)
        if not version:
            raise GovernanceError(f"版本 {version_id} 不存在", 404)
        return version

    def submit_version(self, version_id, actor="editor"):
        with self._lock:
            version = self._require_version(version_id)
            if version["status"] != "draft":
                raise GovernanceError("只有草稿状态可以提交审核", 409)
            version["status"] = "in_review"
            version["submitted_at"] = _iso(self.clock())
            self._event("version.submitted", actor, f"version:{version_id}")
            self.save()
            return copy.deepcopy(version)

    def record_review(self, version_id, role, decision, actor, comment=""):
        """记录三岗意见；运营批准只能在医学与无障碍均通过后给出。"""
        if role not in REVIEW_ROLES:
            raise GovernanceError(f"未知审核岗位 {role}")
        if decision not in ("approved", "changes_requested"):
            raise GovernanceError("decision 只能是 approved 或 changes_requested")
        with self._lock:
            version = self._require_version(version_id)
            if version["status"] != "in_review":
                raise GovernanceError("版本未处于审核中", 409)
            if role != "sports_medicine":
                required = REVIEW_ROLES[: ROLE_ORDER[role]]
                for earlier in required:
                    if version["reviews"].get(earlier, {}).get("decision") != "approved":
                        raise GovernanceError(
                            f"需先完成{ROLE_LABELS[earlier]}，才能进行{ROLE_LABELS[role]}", 409
                        )
            version["reviews"][role] = {
                "decision": decision,
                "actor": actor,
                "comment": comment,
                "at": _iso(self.clock()),
            }
            self._event(
                "version.reviewed",
                actor,
                f"version:{version_id}",
                role=role,
                decision=decision,
                comment=comment,
            )
            self.save()
            return copy.deepcopy(version)

    def _eligible_roles(self, version):
        return [
            role
            for role in REVIEW_ROLES
            if version["reviews"].get(role, {}).get("decision") == "approved"
        ]

    def is_publishable(self, version_id):
        with self._lock:
            version = self._require_version(version_id)
            return self._eligible_roles(version) == list(REVIEW_ROLES)

    def withdraw_review(self, version_id, role, actor, reason):
        """专家撤回意见：未发布则封阻发布；已发布则暂停受影响内容并给出替代提示。"""
        if role not in REVIEW_ROLES:
            raise GovernanceError(f"未知审核岗位 {role}")
        if not reason:
            raise GovernanceError("撤回必须说明原因")
        with self._lock:
            version = self._require_version(version_id)
            if role not in version["reviews"]:
                raise GovernanceError("该岗位尚无意见可撤回", 409)
            del version["reviews"][role]
            self._event(
                "version.review_withdrawn",
                actor,
                f"version:{version_id}",
                role=role,
                reason=reason,
            )
            affected_qrs = [
                release["qr_id"]
                for release in self._state["releases"]
                if release["version_id"] == version_id and release["status"] == "active"
            ]
            if affected_qrs:
                substitute = (
                    "该动作指导因审核意见撤回正在复核，请暂停该动作；"
                    "儿童、老人及伤后恢复者请先咨询现场工作人员。"
                )
                self._suspend_locked(
                    affected_qrs,
                    event_type="expert_withdrawal",
                    reason=f"{ROLE_LABELS[role]}撤回：{reason}",
                    actor=actor,
                    substitute={"kind": "safety_notice", "message": substitute},
                )
            self.save()
            return {"version_id": version_id, "suspended_qr_ids": affected_qrs}

    # ------------------------------------------------------------------ 发布

    def publish(self, qr_id, version_id, actor, reason, idempotency_key=None):
        """发布绑定：三岗齐备才生效；同一二维码的旧发布被标记为被替换并留痕。"""
        with self._lock:
            if idempotency_key:
                cached = self._state["idempotency"].get(f"publish:{idempotency_key}")
                if cached:
                    return copy.deepcopy(cached)
            qr = self._require_qr(qr_id)
            version = self._require_version(version_id)
            approved = self._eligible_roles(version)
            if approved != list(REVIEW_ROLES):
                missing = [ROLE_LABELS[r] for r in REVIEW_ROLES if r not in approved]
                raise GovernanceError(f"发布前还需完成：{'、'.join(missing)}", 409)

            current = self._active_release(qr_id)
            if current and current["version_id"] == version_id:
                # 同一版本重复发布请求是幂等读取，不产生第二条发布。
                result = copy.deepcopy(current)
                if idempotency_key:
                    self._state["idempotency"][f"publish:{idempotency_key}"] = result
                return result

            release = {
                "release_id": self._id("rel"),
                "qr_id": qr_id,
                "facility_id": qr["facility_id"],
                "guide_id": version["guide_id"],
                "version_id": version_id,
                "revision": version["revision"],
                "published_at": _iso(self.clock()),
                "published_by": actor,
                "publish_reason": reason or "",
                "status": "active",
            }
            self._state["releases"].append(release)

            if current:
                current["status"] = "superseded"
                current["replaced_by"] = release["release_id"]
                current["replace_reason"] = reason or ""
                current["superseded_at"] = release["published_at"]
                self._event(
                    "release.superseded",
                    actor,
                    f"qr:{qr_id}",
                    reason=reason,
                    release_id=current["release_id"],
                    version_id=current["version_id"],
                    replaced_by=release["release_id"],
                )
                self._create_notice_locked(
                    qr_id=qr_id,
                    version_id=current["version_id"],
                    kind="superseded",
                    reason=reason or f"由 {version_id} 替换",
                    substitute={"new_version_id": version_id},
                )

            self._event(
                "release.published",
                actor,
                f"qr:{qr_id}",
                reason=reason,
                release_id=release["release_id"],
                version_id=version_id,
                facility_id=qr["facility_id"],
            )
            if idempotency_key:
                self._state["idempotency"][f"publish:{idempotency_key}"] = copy.deepcopy(release)
            self.save()
            return copy.deepcopy(release)

    def _active_release(self, qr_id):
        for release in reversed(self._state["releases"]):
            if release["qr_id"] == qr_id and release["status"] == "active":
                return release
        return None

    def _suspended_release(self, qr_id):
        for release in reversed(self._state["releases"]):
            if release["qr_id"] == qr_id and release["status"] == "suspended":
                return release
        return None

    # ------------------------------------------------- 迁移 / 换件 / 风险 / 恢复

    def relocate_facility(self, facility_id, new_site, actor, reason):
        """设施迁移：暂停该设施下全部二维码内容，直到现场复核重新发布。"""
        with self._lock:
            facility = self._state["facilities"].get(facility_id)
            if not facility:
                raise GovernanceError(f"设施 {facility_id} 不存在", 404)
            old_site = facility["site"]
            facility["site"] = new_site
            facility["state"] = "relocated"
            qr_ids = self._qrs_of_facility(facility_id)
            self._suspend_locked(
                qr_ids,
                "relocation",
                reason or f"设施由 {old_site} 迁移至 {new_site}",
                actor,
                {"kind": "relocation", "message": "设施已迁移，动作指导正在现场复核，请暂停使用或咨询工作人员。"},
            )
            self.save()
            return {"facility_id": facility_id, "suspended_qr_ids": qr_ids}

    def replace_component(self, facility_id, new_model, actor, reason):
        """部件更换（可能换型）：型号改变后旧指导不再保证适用，定向暂停。"""
        with self._lock:
            facility = self._state["facilities"].get(facility_id)
            if not facility:
                raise GovernanceError(f"设施 {facility_id} 不存在", 404)
            old_model = facility["model"]
            facility["model"] = new_model
            facility["state"] = "component_replaced"
            qr_ids = self._qrs_of_facility(facility_id)
            self._suspend_locked(
                qr_ids,
                "component_replaced",
                reason or f"部件更换，型号 {old_model} → {new_model}",
                actor,
                {"kind": "model_change", "message": f"器材已更换为 {new_model}，旧动作指导暂停，请勿照旧操作。"},
            )
            self.save()
            return {"facility_id": facility_id, "suspended_qr_ids": qr_ids}

    def report_risk_event(self, actor, reason, qr_ids=None, version_id=None, facility_id=None):
        """风险事件：可精确到二维码、版本或整台设施，只暂停受影响范围。"""
        if not reason:
            raise GovernanceError("风险事件必须说明原因")
        with self._lock:
            targets = self._scope_to_qrs(qr_ids, version_id, facility_id)
            self._suspend_locked(
                targets,
                "risk_event",
                reason,
                actor,
                {"kind": "safety_notice", "message": "收到安全风险报告，该指导暂停，请停止相关动作并联系现场人员。"},
                version_id=version_id,
            )
            self.save()
            return {"suspended_qr_ids": targets}

    def _qrs_of_facility(self, facility_id):
        return [
            qr_id
            for qr_id, qr in self._state["qrcodes"].items()
            if qr["facility_id"] == facility_id
        ]

    def _scope_to_qrs(self, qr_ids, version_id, facility_id):
        targets = []
        if qr_ids:
            for qr_id in qr_ids:
                self._require_qr(qr_id)
                if qr_id not in targets:
                    targets.append(qr_id)
        if version_id:
            self._require_version(version_id)
            for release in self._state["releases"]:
                if release["version_id"] == version_id and release["status"] == "active":
                    if release["qr_id"] not in targets:
                        targets.append(release["qr_id"])
        if facility_id:
            if facility_id not in self._state["facilities"]:
                raise GovernanceError(f"设施 {facility_id} 不存在", 404)
            for qr_id in self._qrs_of_facility(facility_id):
                if qr_id not in targets:
                    targets.append(qr_id)
        if not targets:
            raise GovernanceError("暂停范围为空：至少提供 qr_ids / version_id / facility_id 之一")
        return targets

    def _suspend_locked(self, qr_ids, event_type, reason, actor, substitute, version_id=None):
        batch = {
            "suspension_id": self._id("sus"),
            "event_type": event_type,
            "reason": reason,
            "substitute": substitute,
            "actor": actor,
            "at": _iso(self.clock()),
            "qr_ids": [],
        }
        for qr_id in qr_ids:
            release = self._active_release(qr_id)
            if not release:
                continue  # 本就没有生效内容，不制造暂停记录
            release["status"] = "suspended"
            release["suspended_at"] = batch["at"]
            release["suspension_event"] = event_type
            release["suspension_reason"] = reason
            release["substitute"] = substitute
            batch["qr_ids"].append(qr_id)
            self._event(
                "release.suspended",
                actor,
                f"qr:{qr_id}",
                reason=reason,
                event_type=event_type,
                release_id=release["release_id"],
                version_id=release["version_id"],
            )
            self._create_notice_locked(
                qr_id=qr_id,
                version_id=version_id or release["version_id"],
                kind="suspended",
                reason=reason,
                substitute=substitute,
            )
        if batch["qr_ids"]:
            self._state["suspensions"].append(batch)

    def reinstate_release(self, qr_id, actor, reason):
        """现场复核确认无碍后恢复（迁移后型号未变、风险解除等），全程留痕。"""
        if not reason:
            raise GovernanceError("恢复必须留痕说明依据")
        with self._lock:
            release = self._suspended_release(qr_id)
            if not release:
                raise GovernanceError("该二维码没有已暂停的发布可恢复", 409)
            active = self._active_release(qr_id)
            if active:
                raise GovernanceError("当前已有新的生效发布，旧发布不能直接恢复；如需回退请显式重新发布", 409)
            release["status"] = "active"
            release["reinstated_at"] = _iso(self.clock())
            release.pop("suspension_event", None)
            facility = self._state["facilities"].get(release["facility_id"])
            if facility and facility["state"] != "active":
                facility["state"] = "active"
            self._event("release.reinstated", actor, f"qr:{qr_id}", reason=reason, release_id=release["release_id"])
            self._create_notice_locked(
                qr_id=qr_id,
                version_id=release["version_id"],
                kind="reinstated",
                reason=reason,
                substitute={"message": "现场复核完成，原动作指导已恢复。"},
            )
            self.save()
            return copy.deepcopy(release)

    # ------------------------------------------------------------ 纠正通知

    def _create_notice_locked(self, qr_id, version_id, kind, reason, substitute):
        # 同一二维码 + 同一旧版本 + 同一类型，只保留一条未结通知，避免重复打扰。
        for notice in self._state["notices"]:
            if (
                notice["qr_id"] == qr_id
                and notice["version_id"] == version_id
                and notice["kind"] == kind
                and notice["status"] == "active"
            ):
                return notice
        notice = {
            "notice_id": self._id("ntc"),
            "qr_id": qr_id,
            "version_id": version_id,
            "kind": kind,  # suspended / superseded / reinstated
            "reason": reason,
            "substitute": substitute,
            "status": "active",
            "created_at": _iso(self.clock()),
        }
        self._state["notices"].append(notice)
        self._event(
            "notice.created",
            "system",
            f"qr:{qr_id}",
            reason=reason,
            notice_id=notice["notice_id"],
            version_id=version_id,
            kind=kind,
        )
        return notice

    def notice_delivery(self, notice_id):
        """核对一条纠正通知在曾缓存旧内容的终端上的送达状态。"""
        with self._lock:
            notice = next((n for n in self._state["notices"] if n["notice_id"] == notice_id), None)
            if not notice:
                raise GovernanceError(f"通知 {notice_id} 不存在", 404)
            terminals = []
            for terminal_id, terminal in self._state["terminals"].items():
                cached = terminal.get("caches", {}).get(notice["qr_id"])
                ever_cached = f"{notice['qr_id']}:{notice['version_id']}" in terminal.get("seen_versions", [])
                if not ever_cached:
                    continue
                if notice_id in terminal.get("acked", []):
                    state = "acked"
                elif notice_id in terminal.get("delivered", []):
                    state = "delivered"
                else:
                    state = "pending"
                terminals.append(
                    {
                        "terminal_id": terminal_id,
                        "state": state,
                        "last_sync_at": terminal.get("last_sync_at"),
                        "currently_cached_version": cached,
                        "cleared": bool(cached and cached != notice["version_id"]),
                    }
                )
            return {
                "notice": copy.deepcopy(notice),
                "terminals": terminals,
                "summary": {
                    "total": len(terminals),
                    "pending": sum(1 for t in terminals if t["state"] == "pending"),
                    "delivered": sum(1 for t in terminals if t["state"] == "delivered"),
                    "acked": sum(1 for t in terminals if t["state"] == "acked"),
                },
            }

    # --------------------------------------------------------------- 市民扫码

    def resolve_qr(self, qr_id):
        """扫码侧只读视图：生效内容、替代提示与缓存核对规则。"""
        with self._lock:
            qr = self._state["qrcodes"].get(qr_id)
            if not qr:
                raise GovernanceError(f"二维码 {qr_id} 不存在", 404)
            facility = self._state["facilities"][qr["facility_id"]]
            active = self._active_release(qr_id)
            payload = {
                "qr_id": qr_id,
                "facility": {
                    "facility_id": facility["facility_id"],
                    "model": facility["model"],
                    "site": facility["site"],
                },
                "server_time": _iso(self.clock()),
                "cache_policy": {
                    # 离线超过该时长不得继续展示；恢复联网后必须先经 /edge/sync 核对。
                    "max_offline_seconds": self._state["config"]["max_offline_seconds"],
                    "must_reconcile_on_reconnect": True,
                },
            }
            if active:
                version = self._state["versions"][active["version_id"]]
                payload.update(
                    {
                        "status": "active",
                        "release_id": active["release_id"],
                        "version_id": active["version_id"],
                        "revision": active["revision"],
                        "effective_since": active["published_at"],
                        "content": copy.deepcopy(version["content"]),
                    }
                )
            else:
                suspended = self._suspended_release(qr_id)
                payload["status"] = "suspended" if suspended else "unavailable"
                payload["message"] = "该器材暂无适用指导，请咨询现场工作人员。"
                if suspended:
                    payload.update(
                        {
                            "release_id": suspended["release_id"],
                            "version_id": suspended["version_id"],
                            "substitute": suspended.get("substitute"),
                            "suspension_reason": suspended.get("suspension_reason"),
                        }
                    )
            return payload

    # ---------------------------------------------------------------- 边缘同步

    def register_terminal(self, terminal_id):
        with self._lock:
            if not terminal_id:
                raise GovernanceError("terminal_id 不能为空")
            terminals = self._state["terminals"]
            if terminal_id not in terminals:
                terminals[terminal_id] = {
                    "registered_at": _iso(self.clock()),
                    "last_sync_at": None,
                    "caches": {},
                    "seen_versions": [],
                    "delivered": [],
                    "acked": [],
                }
                self.save()
            return {"terminal_id": terminal_id, "registered": True}

    def edge_sync(self, terminal_id, idempotency_key, cached):
        """边缘同步。

        * 幂等键重放：同一键直接返回首次结果，不重复记录、不重复通知；
        * 同步本身永不发布，发布只能由管理端三岗通过后发生；
        * 失联恢复后调用方应以响应里的 ``effective`` 权威清单覆盖本地展示决策。
        """
        if not idempotency_key:
            raise GovernanceError("edge_sync 需要 idempotency_key")
        if not isinstance(cached, list):
            raise GovernanceError("cached 必须是数组")
        with self._lock:
            self.register_terminal(terminal_id)
            terminal = self._state["terminals"][terminal_id]
            key = f"sync:{terminal_id}:{idempotency_key}"
            prior = self._state["idempotency"].get(key)
            if prior is not None:
                # 重放不产生任何副作用：不刷新 last_sync，不重复写送达。
                return copy.deepcopy(prior)

            for item in cached:
                qr_id = item.get("qr_id")
                version_id = item.get("version_id")
                if not qr_id:
                    continue
                terminal["caches"][qr_id] = version_id
                marker = f"{qr_id}:{version_id}"
                if version_id and marker not in terminal["seen_versions"]:
                    terminal["seen_versions"].append(marker)

            effective = []
            requested_qrs = [item.get("qr_id") for item in cached if item.get("qr_id")]
            for qr_id in requested_qrs:
                effective.append(self._effective_view(qr_id))

            corrections = []
            for notice in self._state["notices"]:
                if notice["status"] != "active":
                    continue
                marker = f"{notice['qr_id']}:{notice['version_id']}"
                if marker not in terminal["seen_versions"]:
                    continue
                if notice["notice_id"] in terminal["acked"]:
                    continue
                corrections.append(copy.deepcopy(notice))
                if notice["notice_id"] not in terminal["delivered"]:
                    terminal["delivered"].append(notice["notice_id"])

            response = {
                "terminal_id": terminal_id,
                "server_time": _iso(self.clock()),
                "authoritative": True,
                "effective": effective,
                "corrections": corrections,
                "cache_policy": {
                    "max_offline_seconds": self._state["config"]["max_offline_seconds"],
                    "must_reconcile_on_reconnect": True,
                },
            }
            terminal["last_sync_at"] = response["server_time"]
            self._state["idempotency"][key] = copy.deepcopy(response)
            self._event(
                "edge.synced",
                "terminal",
                f"terminal:{terminal_id}",
                idempotency_key=idempotency_key,
                qr_count=len(effective),
                correction_count=len(corrections),
            )
            self.save()
            return copy.deepcopy(response)

    def _effective_view(self, qr_id):
        active = self._active_release(qr_id)
        if active:
            version = self._state["versions"][active["version_id"]]
            return {
                "qr_id": qr_id,
                "state": "active",
                "release_id": active["release_id"],
                "version_id": active["version_id"],
                "revision": active["revision"],
                "content": copy.deepcopy(version["content"]),
            }
        suspended = self._suspended_release(qr_id)
        if suspended:
            return {
                "qr_id": qr_id,
                "state": "suspended",
                "version_id": suspended["version_id"],
                "substitute": suspended.get("substitute"),
                "reason": suspended.get("suspension_reason"),
            }
        return {"qr_id": qr_id, "state": "unavailable"}

    def ack_notices(self, terminal_id, notice_ids):
        """终端确认纠正通知已在本地应用（旧缓存已撤下）。"""
        with self._lock:
            if terminal_id not in self._state["terminals"]:
                raise GovernanceError(f"终端 {terminal_id} 未注册", 404)
            terminal = self._state["terminals"][terminal_id]
            applied = []
            for notice_id in notice_ids or []:
                if not any(n["notice_id"] == notice_id for n in self._state["notices"]):
                    raise GovernanceError(f"通知 {notice_id} 不存在", 404)
                if notice_id not in terminal["delivered"]:
                    raise GovernanceError(f"通知 {notice_id} 尚未送达，不能确认应用", 409)
                if notice_id not in terminal["acked"]:
                    terminal["acked"].append(notice_id)
                    applied.append(notice_id)
            if applied:
                self._event(
                    "edge.acked",
                    "terminal",
                    f"terminal:{terminal_id}",
                    notice_ids=applied,
                )
                self.save()
            return {"terminal_id": terminal_id, "acked": terminal["acked"][:]}

    # ------------------------------------------------------------- 市民匿名反馈

    def issue_feedback_token(self):
        """发放一次性匿名令牌：不收集身份，只用于同一人的重复提交折叠。"""
        token = _new_token()
        self._state["feedback_tokens"][token] = {"issued_at": _iso(self.clock()), "used_fingerprints": []}
        self.save()
        return {"token": token}

    def submit_feedback(self, token, qr_id, version_id, feedback_type, detail, step_index=None):
        if feedback_type not in FEEDBACK_TYPES:
            raise GovernanceError("type 只能是 unclear 或 cannot_complete")
        if not token or token not in self._state["feedback_tokens"]:
            raise GovernanceError("缺少有效匿名令牌，请先申请", 403)
        with self._lock:
            self._require_qr(qr_id)
            if version_id and version_id not in self._state["versions"]:
                raise GovernanceError(f"版本 {version_id} 不存在", 404)
            if step_index is not None and (not isinstance(step_index, int) or step_index < 0):
                raise GovernanceError("step_index 必须是非负整数")
            normalized = _normalize_detail(detail)
            fingerprint = hashlib.sha256(
                "|".join([qr_id, version_id or "", feedback_type, str(step_index), normalized]).encode("utf-8")
            ).hexdigest()

            token_record = self._state["feedback_tokens"][token]
            if fingerprint in token_record["used_fingerprints"]:
                raise GovernanceError("相同内容你已经提交过，已为你合并，无需重复提交", 409)

            # 跨令牌的短时间完全相同提交（同一设备换令牌刷）折叠到既有条目。
            now = self.clock()
            for prior in reversed(self._state["feedback"]):
                if prior["fingerprint"] == fingerprint and now - prior["created_epoch"] < 3600:
                    prior["dup_count"] += 1
                    token_record["used_fingerprints"].append(fingerprint)
                    self.save()
                    return {"accepted": False, "merged_into": prior["feedback_id"], "feedback_id": prior["feedback_id"]}

            token_record["used_fingerprints"].append(fingerprint)
            entry = {
                "feedback_id": self._id("fb"),
                "token": token,
                "qr_id": qr_id,
                "version_id": version_id,
                "type": feedback_type,
                "step_index": step_index,
                "detail": str(detail or "")[:500],
                "fingerprint": fingerprint,
                "dup_count": 1,
                "created_epoch": now,
                "created_at": _iso(now),
            }
            self._state["feedback"].append(entry)
            self._event(
                "feedback.submitted",
                "anonymous",
                f"version:{version_id}" if version_id else f"qr:{qr_id}",
                feedback_type=feedback_type,
                feedback_id=entry["feedback_id"],
            )
            self.save()
            return {"accepted": True, "feedback_id": entry["feedback_id"]}

    def feedback_summary(self, version_id=None, qr_id=None):
        """反馈汇总：计数按不同匿名令牌（不同提交人），重复提交不扭曲结论。"""
        with self._lock:
            rows = [
                f
                for f in self._state["feedback"]
                if (version_id is None or f["version_id"] == version_id)
                and (qr_id is None or f["qr_id"] == qr_id)
            ]
            unique_tokens = {f["token"] for f in rows}
            by_type = defaultdict(int)
            by_step = defaultdict(int)
            by_qr = defaultdict(int)
            for row in rows:
                by_type[row["type"]] += 1
                if row["step_index"] is not None:
                    by_step[str(row["step_index"])] += 1
                by_qr[row["qr_id"]] += 1
            return {
                "filter": {"version_id": version_id, "qr_id": qr_id},
                "unique_submitters": len(unique_tokens),
                "unique_submissions": len(rows),
                "duplicates_collapsed": sum(r["dup_count"] - 1 for r in rows),
                "by_type": dict(by_type),
                "by_step": dict(by_step),
                "by_qr": dict(by_qr),
                "feedback": [
                    {
                        "feedback_id": r["feedback_id"],
                        "qr_id": r["qr_id"],
                        "version_id": r["version_id"],
                        "type": r["type"],
                        "step_index": r["step_index"],
                        "detail": r["detail"],
                        "created_at": r["created_at"],
                        "dup_count": r["dup_count"],
                    }
                    for r in rows
                ],
            }

    # ----------------------------------------------------------- 查询与时间线

    def list_releases(self, qr_id=None, version_id=None, facility_id=None):
        """列出发布记录（含已替换、已暂停），支撑“对哪些设施生效”的核对。"""
        with self._lock:
            result = []
            for release in self._state["releases"]:
                if qr_id and release["qr_id"] != qr_id:
                    continue
                if version_id and release["version_id"] != version_id:
                    continue
                if facility_id and release["facility_id"] != facility_id:
                    continue
                result.append(copy.deepcopy(release))
            return result

    def timeline(self, qr_id=None, version_id=None, facility_id=None):
        """审计时间线：合并结构化事件，回答何时生效、为何替换、通知是否送达。"""
        with self._lock:
            events = []
            for event in self._state["events"]:
                target = event["target"]
                details = event.get("details", {})
                if qr_id and f"qr:{qr_id}" != target and details.get("qr_id") != qr_id:
                    continue
                if version_id and f"version:{version_id}" != target and details.get("version_id") != version_id:
                    continue
                if facility_id and f"facility:{facility_id}" != target and details.get("facility_id") != facility_id:
                    continue
                events.append(copy.deepcopy(event))
            return {"events": events}

    def get_config(self):
        with self._lock:
            return copy.deepcopy(self._state["config"])
