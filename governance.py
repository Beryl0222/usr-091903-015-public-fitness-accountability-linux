"""扫码健身指导内容治理的领域核心。

把每个二维码与设施型号、安装位置、适用人群、禁忌条件和动作版本绑定，
覆盖三级审核发布、受影响内容暂停与替代提示、边缘终端同步确认、
匿名反馈去重以及全程可核对的审计。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

REVIEW_ROLES = ("sports_medicine", "accessibility", "operations")
REVIEW_DECISIONS = ("approve", "reject", "withdraw")
FACILITY_EVENT_KINDS = ("migrated", "part_replaced", "risk_event")
FEEDBACK_CATEGORIES = ("unclear", "cannot_complete")
FEEDBACK_DEDUP_WINDOW_SECONDS = 24 * 3600

DEFAULT_SUBSTITUTE_NOTICE = "该器材指导内容已暂停，请遵循现场安全提示或咨询工作人员。"


class DomainError(Exception):
    """领域规则被违反时抛出。"""


@dataclass
class Review:
    role: str
    reviewer: str
    decision: str
    note: str
    at: float


@dataclass
class GuidanceVersion:
    guidance_id: str
    version: int
    title: str
    steps: list
    applicable_groups: list
    contraindications: list
    compatible_models: list
    created_at: float
    reviews: dict = field(default_factory=dict)  # role -> Review
    status: str = "draft"  # draft | published | suspended

    def approved(self):
        return all(
            self.reviews.get(role) is not None
            and self.reviews[role].decision == "approve"
            for role in REVIEW_ROLES
        )


@dataclass
class Publication:
    publication_id: str
    qr_code: str
    facility_id: str
    guidance_id: str
    version: int
    effective_from: float
    effective_until: float | None = None
    status: str = "active"  # active | superseded | suspended
    reason: str = ""
    substitute_notice: str = ""


@dataclass
class Facility:
    facility_id: str
    model: str
    park: str
    location: str
    status: str = "active"
    events: list = field(default_factory=list)


@dataclass
class Notice:
    """发给曾缓存旧内容终端的纠正通知。"""

    notice_id: str
    terminal_id: str
    qr_code: str
    guidance_id: str
    old_version: int
    new_version: int | None
    message: str
    created_at: float
    delivered_at: float | None = None
    acknowledged_at: float | None = None


@dataclass
class Terminal:
    terminal_id: str
    online: bool = True
    must_reconfirm: bool = False
    cache: dict = field(default_factory=dict)  # qr_code -> version
    last_sync_at: float | None = None


@dataclass
class Feedback:
    qr_code: str
    guidance_version: int | None
    step_index: int
    category: str
    reporter_token: str
    at: float
    counted: bool


class GovernanceService:
    """指导内容治理后端，全部状态保存在内存中，便于联调与测试。"""

    def __init__(self):
        self.facilities = {}
        self.qr_bindings = {}  # qr_code -> facility_id
        self.guidance = {}  # guidance_id -> {version -> GuidanceVersion}
        self.publications = []  # 全部发布记录，含已失效，供审计
        self.terminals = {}
        self.notices = []
        self.feedback = []
        self.exposures = []  # 终端同步观察到的 (terminal, qr, version, at)，即浏览范围
        self._seq = 0

    def _now(self, now):
        return time.time() if now is None else now

    def _next_id(self, prefix):
        self._seq += 1
        return f"{prefix}-{self._seq:06d}"

    # ---- 设施与二维码绑定 -------------------------------------------------

    def register_facility(self, facility_id, model, park, location):
        if facility_id in self.facilities:
            raise DomainError(f"设施已存在: {facility_id}")
        facility = Facility(facility_id=facility_id, model=model, park=park, location=location)
        self.facilities[facility_id] = facility
        return facility

    def bind_qr(self, qr_code, facility_id):
        if facility_id not in self.facilities:
            raise DomainError(f"未知设施: {facility_id}")
        self.qr_bindings[qr_code] = facility_id
        return {"qr_code": qr_code, "facility_id": facility_id}

    # ---- 指导内容与三级审核 ----------------------------------------------

    def create_guidance(self, guidance_id, title, steps, applicable_groups,
                        contraindications, compatible_models, now=None):
        if guidance_id in self.guidance:
            raise DomainError(f"指导内容已存在: {guidance_id}")
        self.guidance[guidance_id] = {}
        return self.add_version(guidance_id, title, steps, applicable_groups,
                                contraindications, compatible_models, now=now)

    def add_version(self, guidance_id, title, steps, applicable_groups,
                    contraindications, compatible_models, now=None):
        if guidance_id not in self.guidance:
            raise DomainError(f"未知指导内容: {guidance_id}")
        if not steps:
            raise DomainError("动作步骤不能为空")
        version_no = len(self.guidance[guidance_id]) + 1
        version = GuidanceVersion(
            guidance_id=guidance_id,
            version=version_no,
            title=title,
            steps=list(steps),
            applicable_groups=list(applicable_groups),
            contraindications=list(contraindications),
            compatible_models=list(compatible_models),
            created_at=self._now(now),
        )
        self.guidance[guidance_id][version_no] = version
        return version

    def get_version(self, guidance_id, version_no):
        versions = self.guidance.get(guidance_id)
        if not versions or version_no not in versions:
            raise DomainError(f"未知版本: {guidance_id} v{version_no}")
        return versions[version_no]

    def submit_review(self, guidance_id, version_no, role, decision, reviewer,
                      note="", now=None):
        if role not in REVIEW_ROLES:
            raise DomainError(f"未知审核角色: {role}")
        if decision not in REVIEW_DECISIONS:
            raise DomainError(f"未知审核结论: {decision}")
        version = self.get_version(guidance_id, version_no)
        at = self._now(now)
        version.reviews[role] = Review(role=role, reviewer=reviewer,
                                       decision=decision, note=note, at=at)
        if decision == "withdraw" and version.status == "published":
            # 专家撤回意见：只暂停该版本对应的内容，并生成替代提示
            self._suspend_version(
                version,
                reason=f"专家撤回意见({role}/{reviewer})",
                substitute=DEFAULT_SUBSTITUTE_NOTICE,
                now=at,
            )
        return version.reviews[role]

    # ---- 发布（幂等） ------------------------------------------------------

    def publish(self, guidance_id, version_no, now=None):
        version = self.get_version(guidance_id, version_no)
        if version.status == "published":
            # 重复发布（如边缘侧重试）直接返回既有结果，不产生新记录
            return self._active_publications(guidance_id, version_no)
        if version.status == "suspended":
            raise DomainError("该版本已被暂停，不能重新发布，请新建版本")
        if not version.approved():
            missing = [r for r in REVIEW_ROLES
                       if version.reviews.get(r) is None
                       or version.reviews[r].decision != "approve"]
            raise DomainError(f"审核未完成，缺少: {', '.join(missing)}")
        at = self._now(now)
        created = []
        for qr_code, facility_id in self.qr_bindings.items():
            facility = self.facilities[facility_id]
            if facility.status != "active":
                continue
            if facility.model not in version.compatible_models:
                continue
            # 同一二维码上同一指导的旧版本失效，保留历史供核对
            for pub in self._active_publications(guidance_id, None, qr_code=qr_code):
                pub.status = "superseded"
                pub.effective_until = at
                pub.reason = f"被 v{version_no} 替换"
                self._notify_terminals(pub, new_version=version_no,
                                       message=f"{qr_code} 指导已更新至 v{version_no}",
                                       now=at)
            pub = Publication(
                publication_id=self._next_id("pub"),
                qr_code=qr_code,
                facility_id=facility_id,
                guidance_id=guidance_id,
                version=version_no,
                effective_from=at,
            )
            self.publications.append(pub)
            created.append(pub)
        version.status = "published"
        return created

    def _active_publications(self, guidance_id, version_no, qr_code=None):
        return [
            p for p in self.publications
            if p.status == "active"
            and (guidance_id is None or p.guidance_id == guidance_id)
            and (version_no is None or p.version == version_no)
            and (qr_code is None or p.qr_code == qr_code)
        ]

    # ---- 设施事件与局部暂停 ------------------------------------------------

    def facility_event(self, facility_id, kind, detail="", substitute=None, now=None):
        if kind not in FACILITY_EVENT_KINDS:
            raise DomainError(f"未知设施事件: {kind}")
        facility = self.facilities.get(facility_id)
        if facility is None:
            raise DomainError(f"未知设施: {facility_id}")
        at = self._now(now)
        facility.events.append({"kind": kind, "detail": detail, "at": at})
        if kind == "migrated" and detail:
            facility.location = detail
        notice_text = substitute or DEFAULT_SUBSTITUTE_NOTICE
        suspended = []
        for qr_code, bound_facility in self.qr_bindings.items():
            if bound_facility != facility_id:
                continue  # 只暂停受影响设施上的内容
            for pub in list(self._active_publications(None, None, qr_code=qr_code)):
                self._suspend_publication(pub, reason=f"设施事件:{kind}",
                                          substitute=notice_text, now=at)
                suspended.append(pub)
        return suspended

    def _suspend_version(self, version, reason, substitute, now):
        version.status = "suspended"
        for pub in list(self._active_publications(version.guidance_id, version.version)):
            self._suspend_publication(pub, reason=reason, substitute=substitute, now=now)

    def _suspend_publication(self, pub, reason, substitute, now):
        pub.status = "suspended"
        pub.effective_until = now
        pub.reason = reason
        pub.substitute_notice = substitute
        self._notify_terminals(pub, new_version=None, message=substitute, now=now)

    # ---- 边缘终端同步 ------------------------------------------------------

    def _notify_terminals(self, pub, new_version, message, now):
        """向缓存过旧版本的终端生成纠正通知；同一终端同一二维码只保留一条待送达通知。"""
        for terminal in self.terminals.values():
            if terminal.cache.get(pub.qr_code) != pub.version:
                continue
            pending = [n for n in self.notices
                       if n.terminal_id == terminal.terminal_id
                       and n.qr_code == pub.qr_code
                       and n.acknowledged_at is None]
            if pending:
                notice = pending[0]
                notice.new_version = new_version
                notice.message = message
                notice.delivered_at = None
                continue
            self.notices.append(Notice(
                notice_id=self._next_id("notice"),
                terminal_id=terminal.terminal_id,
                qr_code=pub.qr_code,
                guidance_id=pub.guidance_id,
                old_version=pub.version,
                new_version=new_version,
                message=message,
                created_at=now,
            ))

    def mark_offline(self, terminal_id):
        terminal = self.terminals.setdefault(terminal_id, Terminal(terminal_id))
        terminal.online = False
        return terminal

    def terminal_sync(self, terminal_id, known, now=None):
        """终端上报本地缓存，返回各二维码当前有效内容；同步是幂等的，不会触发发布。"""
        at = self._now(now)
        terminal = self.terminals.setdefault(terminal_id, Terminal(terminal_id))
        if not terminal.online:
            # 失联恢复：必须先确认当前有效版本才能继续提供服务
            terminal.online = True
            terminal.must_reconfirm = True
        terminal.cache.update(known)
        terminal.last_sync_at = at
        for qr_code, version_no in terminal.cache.items():
            self.exposures.append({
                "terminal_id": terminal_id, "qr_code": qr_code,
                "version": version_no, "at": at,
            })
        entries = {qr: self.current_for_qr(qr) for qr in terminal.cache}
        pending = [n for n in self.notices
                   if n.terminal_id == terminal_id and n.acknowledged_at is None]
        for notice in pending:
            if notice.delivered_at is None:
                notice.delivered_at = at
        return {
            "terminal_id": terminal_id,
            "must_reconfirm": terminal.must_reconfirm,
            "current": entries,
            "notices": [self._notice_view(n) for n in pending],
        }

    def terminal_confirm(self, terminal_id, accepted, now=None):
        """失联恢复后确认将对外提供的版本；与当前有效版本不一致则拒绝。"""
        terminal = self.terminals.get(terminal_id)
        if terminal is None:
            raise DomainError(f"未知终端: {terminal_id}")
        mismatches = {}
        for qr_code in terminal.cache:
            current = self._active_publication_for_qr(qr_code)
            expected = current.version if current else None
            if accepted.get(qr_code) != expected:
                mismatches[qr_code] = {"expected": expected,
                                       "accepted": accepted.get(qr_code)}
        if mismatches:
            raise DomainError(f"确认版本与当前有效版本不一致: {mismatches}")
        terminal.must_reconfirm = False
        terminal.cache = dict(accepted)
        terminal.last_sync_at = self._now(now)
        return {"terminal_id": terminal_id, "must_reconfirm": False}

    def ack_notice(self, terminal_id, notice_id, now=None):
        for notice in self.notices:
            if notice.notice_id == notice_id and notice.terminal_id == terminal_id:
                notice.acknowledged_at = self._now(now)
                return notice
        raise DomainError(f"未知通知: {notice_id}")

    def _active_publication_for_qr(self, qr_code):
        active = [p for p in self.publications
                  if p.qr_code == qr_code and p.status == "active"]
        return active[-1] if active else None

    def current_for_qr(self, qr_code):
        """市民扫码当前应看到的内容：有效版本、替代提示或空。"""
        pub = self._active_publication_for_qr(qr_code)
        if pub is not None:
            version = self.get_version(pub.guidance_id, pub.version)
            return {
                "status": "effective",
                "qr_code": qr_code,
                "guidance_id": pub.guidance_id,
                "version": pub.version,
                "title": version.title,
                "steps": version.steps,
                "applicable_groups": version.applicable_groups,
                "contraindications": version.contraindications,
                "effective_from": pub.effective_from,
            }
        suspended = [p for p in self.publications
                     if p.qr_code == qr_code and p.status == "suspended"]
        if suspended:
            latest = suspended[-1]
            return {
                "status": "suspended",
                "qr_code": qr_code,
                "guidance_id": latest.guidance_id,
                "version": latest.version,
                "substitute_notice": latest.substitute_notice,
                "reason": latest.reason,
            }
        return {"status": "none", "qr_code": qr_code}

    # ---- 匿名反馈（去重防扭曲） --------------------------------------------

    def submit_feedback(self, qr_code, step_index, category, reporter_token,
                        guidance_version=None, now=None):
        if category not in FEEDBACK_CATEGORIES:
            raise DomainError(f"未知反馈类别: {category}")
        if not reporter_token:
            raise DomainError("缺少匿名报告标识")
        at = self._now(now)
        counted = not any(
            f.counted
            and f.reporter_token == reporter_token
            and f.qr_code == qr_code
            and f.step_index == step_index
            and f.category == category
            and at - f.at < FEEDBACK_DEDUP_WINDOW_SECONDS
            for f in self.feedback
        )
        entry = Feedback(qr_code=qr_code, guidance_version=guidance_version,
                         step_index=step_index, category=category,
                         reporter_token=reporter_token, at=at, counted=counted)
        self.feedback.append(entry)
        return entry

    def feedback_summary(self, qr_code):
        """按步骤汇总；重复提交只计一次，避免少量刷票扭曲结论。"""
        steps = {}
        for f in self.feedback:
            if f.qr_code != qr_code:
                continue
            bucket = steps.setdefault(f.step_index, {})
            stat = bucket.setdefault(f.category, {
                "submitted": 0, "counted": 0, "unique_reporters": set(),
            })
            stat["submitted"] += 1
            if f.counted:
                stat["counted"] += 1
                stat["unique_reporters"].add(f.reporter_token)
        return {
            str(step): {
                category: {
                    "submitted": stat["submitted"],
                    "counted": stat["counted"],
                    "unique_reporters": len(stat["unique_reporters"]),
                }
                for category, stat in bucket.items()
            }
            for step, bucket in steps.items()
        }

    # ---- 审计 --------------------------------------------------------------

    def audit_guidance(self, guidance_id):
        """一条指导何时对哪些设施生效、为什么被替换、影响范围到达哪些终端。"""
        if guidance_id not in self.guidance:
            raise DomainError(f"未知指导内容: {guidance_id}")
        versions = []
        for version_no in sorted(self.guidance[guidance_id]):
            version = self.guidance[guidance_id][version_no]
            pubs = [p for p in self.publications
                    if p.guidance_id == guidance_id and p.version == version_no]
            versions.append({
                "version": version_no,
                "status": version.status,
                "reviews": {role: {"decision": r.decision, "reviewer": r.reviewer,
                                   "note": r.note, "at": r.at}
                            for role, r in version.reviews.items()},
                "publications": [{
                    "publication_id": p.publication_id,
                    "qr_code": p.qr_code,
                    "facility_id": p.facility_id,
                    "effective_from": p.effective_from,
                    "effective_until": p.effective_until,
                    "status": p.status,
                    "reason": p.reason,
                    "substitute_notice": p.substitute_notice,
                    "reached_terminals": sorted({
                        e["terminal_id"] for e in self.exposures
                        if e["qr_code"] == p.qr_code and e["version"] == version_no
                    }),
                } for p in pubs],
            })
        return {"guidance_id": guidance_id, "versions": versions}

    def audit_notices(self):
        """纠正通知是否到达曾缓存旧内容的终端。"""
        return [self._notice_view(n) for n in self.notices]

    @staticmethod
    def _notice_view(notice):
        return {
            "notice_id": notice.notice_id,
            "terminal_id": notice.terminal_id,
            "qr_code": notice.qr_code,
            "guidance_id": notice.guidance_id,
            "old_version": notice.old_version,
            "new_version": notice.new_version,
            "message": notice.message,
            "created_at": notice.created_at,
            "delivered_at": notice.delivered_at,
            "acknowledged_at": notice.acknowledged_at,
        }
