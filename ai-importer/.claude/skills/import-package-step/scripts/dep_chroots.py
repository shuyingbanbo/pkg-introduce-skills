#!/usr/bin/env python3
"""dep_registry per-chroot 就绪状态的共享 helper（多 chroot 构建）。

Schema：条目保留包级字段（url/constraint/required_by/status），新增可选键
    "chroots": {<chroot>: {"status": ..., "build_id": <int 可选>}}

per-chroot status 词表：pending / building / build_done / failed / reused / skipped。
包级 status 是派生聚合字段（aggregate_status），只给旧消费者读；无 chroots 键的
旧条目一律按单 chroot 旧格式处理，行为与引入本模块前完全一致。

使用方：step_supervisor.py（就绪谓词 / poll 更新 / pending_deps 晋升）、
notify_job.py（chroot_status 聚合回写）等。
"""

from __future__ import annotations

# per-chroot 上算"已就绪"的状态
CHROOT_READY_STATUSES = ("build_done", "reused")

# 包级（旧格式）算"已就绪"的状态；与 step_supervisor.py 的 DEP_READY_STATUSES 对齐
PKG_READY_STATUSES = ("build_done", "reused", "vendor_only")

# per-chroot 合法词表（§8.1）
CHROOT_STATUSES = ("pending", "building", "build_done", "failed", "reused", "skipped")


def ready_for(entry: dict, chroot: str) -> bool:
    """判断依赖 entry 对指定 chroot 是否已就绪（§8.1 就绪谓词）。

    - vendor_only 恒就绪：产物 vendor 进 SRPM，与 chroot 无关；
    - 有 chroots 键：看 chroots[chroot].status ∈ {build_done, reused}
      （chroot 不在映射中 = 尚未提交，不就绪）；
    - 无 chroots 键（旧条目）：退化为包级 status ∈ {build_done, reused, vendor_only}。
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("status") == "vendor_only":
        return True
    chroots = entry.get("chroots")
    if isinstance(chroots, dict):
        cinfo = chroots.get(chroot)
        if not isinstance(cinfo, dict):
            return False
        return cinfo.get("status") in CHROOT_READY_STATUSES
    return entry.get("status") in PKG_READY_STATUSES


def aggregate_status(entry: dict, target_chroots) -> str:
    """按 §8.1 聚合规则从 per-chroot 状态派生包级 status。

    规则（只看 target_chroots 里的 chroot，映射中缺失的按 pending 计）：
      - 任一 failed                                   → "failed"
      - 其余全部 ∈ {build_done, reused, skipped}
        且至少一个 ∈ {build_done, reused}             → "build_done"
        （部分成功 + 部分 skipped 也算 build_done，skipped 由报告标注）
      - 任一 building                                 → "building"
      - 其余                                          → "pending"

    无 chroots 键的旧条目原样返回包级 status；vendor_only 条目恒返回
    "vendor_only"（chroot 无关终态，不被 per-chroot 状态覆盖）。
    """
    if not isinstance(entry, dict):
        return "pending"
    pkg_status = entry.get("status", "")
    if pkg_status == "vendor_only":
        return "vendor_only"
    chroots = entry.get("chroots")
    if not isinstance(chroots, dict):
        return pkg_status or "pending"

    statuses = []
    for c in target_chroots:
        cinfo = chroots.get(c)
        if isinstance(cinfo, dict):
            statuses.append(cinfo.get("status", "pending"))
        else:
            statuses.append("pending")

    if any(s == "failed" for s in statuses):
        return "failed"
    if statuses and all(s in CHROOT_READY_STATUSES + ("skipped",) for s in statuses):
        if any(s in CHROOT_READY_STATUSES for s in statuses):
            return "build_done"
        # 全部 skipped：没有任何 chroot 产出，视为 failed
        return "failed"
    if any(s == "building" for s in statuses):
        return "building"
    return "pending"


def chroot_status_map(entry: dict) -> dict:
    """提取条目的 {chroot: {"status", "build_id"}} 映射（build_id 缺省为 None）。

    无 chroots 键时返回 {}——调用方据此跳过 chroot 相关字段，保持旧格式输出不变。
    """
    chroots = entry.get("chroots") if isinstance(entry, dict) else None
    if not isinstance(chroots, dict):
        return {}
    out = {}
    for c, cinfo in chroots.items():
        if isinstance(cinfo, dict):
            out[c] = {"status": cinfo.get("status", "pending"),
                      "build_id": cinfo.get("build_id")}
    return out
