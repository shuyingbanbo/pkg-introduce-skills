#!/usr/bin/env python3
"""import-package-step 状态机。

读取 session 状态，输出下一步 action，并在 action 完成后更新状态。

用法：
  # 读状态，输出 action
  python3 step_supervisor.py --session-dir /path/to/session

  # action 完成后更新状态
  python3 step_supervisor.py --session-dir /path/to/session \
      --update-action build_dep --update-target dj-static \
      --build-result success --ci-status pass

  # 标记 dep 为 reused（evaluate 完成后）
  python3 step_supervisor.py --session-dir /path/to/session \
      --update-action evaluate --update-target static3 \
      --gate-decision reuse_official

输出 JSON：
  {"action": "build_dep", "target": "dj-static", "delay": 60, "loop": 6}
  {"action": "done", "target": "sites-faciles", "delay": null, "loop": 10}
  {"action": "fail", "target": "dep build_failed: [...]", "delay": null, "loop": 3}
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

# timeline 事件写入（快照 diff）
from timeline import _snapshot_statuses, diff_and_write_transitions

# dep_registry per-chroot 就绪谓词/聚合（多 chroot，设计 §8.1）；脚本与本文件同目录
try:
    from dep_chroots import ready_for as _ready_for, aggregate_status as _aggregate_status
except ImportError:  # 兜底：dep_chroots.py 未部署时的内联实现（词表与 dep_chroots 保持一致）
    def _ready_for(entry: dict, chroot: str) -> bool:
        if not isinstance(entry, dict):
            return False
        if entry.get("status") == "vendor_only":
            return True
        chroots = entry.get("chroots")
        if isinstance(chroots, dict):
            cinfo = chroots.get(chroot)
            return isinstance(cinfo, dict) and cinfo.get("status") in ("build_done", "reused")
        return entry.get("status") in DEP_READY_STATUSES

    def _aggregate_status(entry: dict, target_chroots) -> str:
        if not isinstance(entry, dict):
            return "pending"
        pkg_status = entry.get("status", "")
        if pkg_status == "vendor_only":
            return "vendor_only"
        chroots = entry.get("chroots")
        if not isinstance(chroots, dict):
            return pkg_status or "pending"
        statuses = [chroots[c].get("status", "pending") if isinstance(chroots.get(c), dict)
                    else "pending" for c in target_chroots]
        if any(s == "failed" for s in statuses):
            return "failed"
        if statuses and all(s in ("build_done", "reused", "skipped") for s in statuses):
            return "build_done" if any(s in ("build_done", "reused") for s in statuses) else "failed"
        if any(s == "building" for s in statuses):
            return "building"
        return "pending"

# build_rpm_result.json 的合法终态
# precheck_done  — 预检通过但构建未完成（agent 中断），视为"待构建"
# interrupted    — agent 异常退出，视为"待构建"
# copr_running   — COPR 构建已提交但 wait_for_build 超时，build_id 已记录，supervisor 轮询
VALID_BUILD_STATUSES = {
    "success", "dep_needed", "failed", "ci_failed", "precheck_done", "interrupted", "copr_running"
}

# dep_registry 中表示"已就绪"的状态（等价于 build_done）
# vendor_only — crate/module 类依赖，无 RPM 产物，可用性由父包 vendor 保证
DEP_READY_STATUSES = {"build_done", "reused", "vendor_only"}

# dep_registry 中表示"等待自身前置依赖就绪"的状态
DEP_WAITING_STATUS = "pending_deps"

# ── 多 chroot 支持 ──
# 一个 job 一个 COPR build 覆盖 N 个 chroot；失败只增量重交失败的 chroot；
# 依赖就绪按 chroot 判定。所有 per-chroot 逻辑只在"新 session"（session.json 显式
# 带 copr_chroots 列表）下启用；旧 session（仅 copr_chroot 单值）与无 chroots 键的
# dep_registry 条目一律走旧路径，端到端行为不变。

# 聚合状态（dep_chroots 词表）→ 包级旧词表映射：包级 status 保持旧词表
# （build_failed/copr_running/build_done），job_runner 日志回收、进度展示等
# 旧消费者无需感知 per-chroot 维度；新消费者（notify_job 等）直接读 chroots 映射。
_AGG_TO_PKG_STATUS = {"build_done": "build_done", "failed": "build_failed", "building": "copr_running"}

# per-chroot 终态（不再重复提交；building 不在此列——崩溃恢复时允许重交覆盖）
_CHROOT_CLOSED_STATUSES = {"build_done", "reused", "skipped"}

# determine_action 派发附加信息（本轮可提交 chroot 子集 / 本轮失败 chroot），
# 由 main() 读取并以 COPR_BUILD_CHROOTS / CHROOT 键输出，供 job_runner 注入环境变量。
_DISPATCH_EXTRA: dict = {}


def _read_session(sd: Path) -> dict:
    try:
        return read_json(sd / "session.json")
    except Exception:
        return {}


def _target_chroots(sd: Path) -> list[str]:
    """本 job 的目标 chroot 集合：session.copr_chroots(list) 优先，fallback 旧单值 copr_chroot。"""
    s = _read_session(sd)
    chroots = s.get("copr_chroots")
    if isinstance(chroots, list) and chroots:
        return [str(c) for c in chroots if c]
    single = s.get("copr_chroot", "")
    return [single] if single else []


def _chroot_tracking(sd: Path) -> bool:
    """是否启用 per-chroot 记账：仅新 session（session.json 显式带 copr_chroots 列表）。"""
    s = _read_session(sd)
    return isinstance(s.get("copr_chroots"), list) and bool(s.get("copr_chroots"))


def _refresh_pkg_status(entry: dict, targets: list[str]) -> str:
    """按 chroots 映射重算包级 status（旧词表，§8.1 聚合规则）。

    聚合为 pending（部分 chroot 未提交/依赖未就绪）时映射为 evaluate_done——
    回到 Priority 2 提交门控重算可提交子集 S，依赖增量重交成功后下一轮晋升补交。
    """
    if entry.get("status") == "vendor_only":
        return "vendor_only"
    agg = _aggregate_status(entry, targets)
    return _AGG_TO_PKG_STATUS.get(agg, "evaluate_done")


def _blockers_of(reg: dict, pkgname: str) -> list[str]:
    """pkgname 的前置依赖（required_by 指向它的条目）。"""
    return [k for k, v in reg.items()
            if isinstance(v, dict) and v.get("required_by") == pkgname]


def _submittable_chroots(reg: dict, pkgname: str, targets: list[str],
                         blockers: list[str] | None = None) -> list[str]:
    """提交门控（§8.1）：S(B) = {c ∈ targets | B 在 c 未终态 且 B 的每个依赖 ready_for(d, c)}。

    blockers 缺省取 required_by 链（dep 用）；主包调用方传全部 reg 键。
    旧条目（无 chroots 键）的 ready_for 退化为包级 status 判断，等价单 chroot 旧行为。
    """
    entry = reg.get(pkgname, {})
    chroots = entry.get("chroots") if isinstance(entry.get("chroots"), dict) else {}
    if blockers is None:
        blockers = _blockers_of(reg, pkgname)
    return [c for c in targets
            if not (isinstance(chroots.get(c), dict)
                    and chroots[c].get("status") in _CHROOT_CLOSED_STATUSES)
            and all(_ready_for(reg.get(b, {}), c) for b in blockers)]


def _apply_doomed_chroots(reg: dict, pkgname: str, targets: list[str]) -> bool:
    """skip 级联：前置依赖在某 chroot 已 skipped（终态放弃）→ 本包该 chroot 同样
    置 skipped（否则会永远等不到依赖就绪，白耗到 job 超时）。返回是否有改动。"""
    doomed: set[str] = set()
    for b in _blockers_of(reg, pkgname):
        bch = reg.get(b, {}).get("chroots")
        if isinstance(bch, dict):
            doomed.update(c for c in targets
                          if isinstance(bch.get(c), dict) and bch[c].get("status") == "skipped")
    if not doomed:
        return False
    entry = reg[pkgname]
    ch = entry.setdefault("chroots", {})
    changed = False
    for c in sorted(doomed):
        cur = ch.get(c)
        if not (isinstance(cur, dict) and cur.get("status") in _CHROOT_CLOSED_STATUSES):
            ch[c] = {"status": "skipped",
                     "build_id": cur.get("build_id") if isinstance(cur, dict) else None}
            changed = True
    if changed:
        entry["status"] = _refresh_pkg_status(entry, targets)
        if entry["status"] == "build_failed":
            entry["error"] = f"前置依赖在 chroot {sorted(doomed)} 已 skipped，级联放弃"
    return changed

# vendor 语言闭集：crate/module 由父包 vendor 解决，不打独立 RPM。
# C/C++ 库依赖不在此列（走 BuildRequires + 独立 RPM 正路）。
VENDOR_LANGS = {"go", "rust"}

# 编译慢的语言，用较长延迟
SLOW_LANGS = {"rust", "go", "c", "cpp"}

# 上游地址解析脚本（优先脚本直查，失败再走 AI）
_RESOLVE_SCRIPT = Path(__file__).resolve().parent / "resolve_upstream.py"



def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _copr_api_get(sd: Path, api_path: str):
    """GET COPR api_3（凭据取自 session.json），失败返回 None。"""
    try:
        session = read_json(sd / "session.json")
        login = session.get("copr_login", "")
        token = session.get("copr_token", "")
        frontend = session.get("copr_url", "http://copr-frontend:5000")
        if not login or not token:
            return None
        import urllib.request, base64
        creds = base64.b64encode(f"{login}:{token}".encode()).decode()
        req = urllib.request.Request(
            f"{frontend}/api_3{api_path}",
            headers={"Authorization": f"Basic {creds}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[warn] COPR API GET {api_path} error: {e}", file=sys.stderr)
        return None


def _normalize_copr_state(state: str) -> str:
    """COPR 状态归一化为 succeeded/failed/running（沿用旧 _poll_copr_build 语义：
    canceled/skipped 等终态非成功一律 failed）。"""
    if state == "succeeded":
        return "succeeded"
    if state in ("failed", "canceled", "skipped"):
        return "failed"
    return "running"


def _poll_copr_build(build_id: int, sd: Path) -> str | None:
    """轮询 COPR build 状态，返回 'succeeded'/'failed'/'running'。
    读取 session.json 里的 COPR 凭据，失败时返回 None。
    （旧单状态路径：build 聚合 state，供单 chroot/主包兼容路径使用。）
    """
    data = _copr_api_get(sd, f"/build/{build_id}")
    if not isinstance(data, dict):
        return None
    return _normalize_copr_state(data.get("state", ""))


def _poll_copr_build_chroots(build_id: int, sd: Path) -> dict[str, str] | None:
    """轮询 COPR build 的逐 chroot 状态，返回 {chroot: 'succeeded'/'failed'/'running'}。

    逐 chroot 状态来自 GET /build-chroot/list/<id>（items[].name/state）；
    该接口无数据时退化为 GET /build/<id> 的聚合 state 映射到该 build 的全部
    chroot（等价旧单状态行为）。完全失败返回 None（调用方按"仍在构建"处理）。
    """
    data = _copr_api_get(sd, f"/build/{build_id}")
    if not isinstance(data, dict):
        return None
    agg = _normalize_copr_state(data.get("state", ""))
    names = [c for c in (data.get("chroots") or []) if isinstance(c, str)]
    listing = _copr_api_get(sd, f"/build-chroot/list/{build_id}")
    items = listing.get("items") if isinstance(listing, dict) else None
    if items:
        out = {}
        for it in items:
            if isinstance(it, dict) and it.get("name"):
                out[it["name"]] = _normalize_copr_state(it.get("state", ""))
        if out:
            return out
    # 兜底：聚合状态应用到该 build 的所有 chroot
    if names:
        return {c: agg for c in names}
    return None


def write_json(path: Path, data: dict[str, Any]) -> None:
    # 目录可能尚不存在（ROS dep 的伪 gate_result 写入时 pkgs/<dep>/ 未建立），
    # 写文件一律先补父目录
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_lang(sd: Path, pkgname: str) -> str:
    gate_f = sd / f"pkgs/{pkgname}/gate_result_{pkgname}.json"
    if gate_f.exists():
        return read_json(gate_f).get("result", {}).get("lang", "")
    return ""


def build_delay(lang: str) -> int:
    return 270 if lang in SLOW_LANGS else 60


# ── vendor_only / crate 身份判定 ─────────────────────────────────────────────
# 拦截依据是"crate 身份"而非"检测语言"：ripgrep（rust 二进制）、pydantic-core
# （python+rust 混合，detect_lang 会判成 rust）都是合法 RPM 包级依赖，误标
# vendor_only 会让父包等一个永远不会出现的 RPM。

def _parent_vendor_crates(sd: Path, parent: str) -> set[str]:
    """读取父包 precheck 结果中的 vendor_crates 清单（crate/module 名集合）。

    precheck 文件可能在 pkgs/<parent>/pre_check.json 或 reports/pre_check_<parent>.json。
    """
    crates: set[str] = set()
    for cand in (sd / "pkgs" / parent / "pre_check.json",
                 sd / "reports" / f"pre_check_{parent}.json"):
        if not cand.exists():
            continue
        try:
            data = read_json(cand)
        except Exception:
            continue
        for names in (data.get("vendor_crates") or {}).values():
            if isinstance(names, list):
                crates.update(str(n) for n in names)
    return crates


def _is_pure_lib_crate(sd: Path, dep: str) -> bool:
    """evaluate 源码证据判定 dep 是否为纯库 crate/module（不可能是合法 RPM 依赖）。

    rust：Cargo.toml 存在、无 [[bin]]、且无其他生态的包级 manifest；
    go：go.mod 存在、无其他生态 manifest、且全仓库无 package main（纯库 module）。
    证据不足时返回 False（保守放行，正常构建）。
    """
    _OTHER_MANIFESTS = ("pyproject.toml", "setup.py", "package.json", "pom.xml", "Gemfile")
    src = sd / "sources" / dep
    if not src.is_dir():
        return False
    if any((src / f).exists() for f in _OTHER_MANIFESTS):
        return False  # 混合包级依赖（如 pydantic-core），不是纯 crate
    cargo = src / "Cargo.toml"
    if cargo.exists():
        try:
            content = cargo.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
        return "[[bin]]" not in content
    if (src / "go.mod").exists():
        for go_file in src.rglob("*.go"):
            try:
                head = go_file.read_text(encoding="utf-8", errors="ignore")[:4096]
            except OSError:
                continue
            if re.search(r"^package main$", head, re.MULTILINE):
                return False
        return True
    return False


def _is_vendor_crate(sd: Path, dep: str, reg: dict) -> bool:
    """crate 身份判定：满足其一即为 crate/module（vendor 解决，不打 RPM）：
    1. dep 名出现在父包 precheck 的 vendor_crates 清单（父包 Cargo.toml/go.mod 声明）；或
    2. gate 检测为 go/rust 且源码证据确认为纯库 crate/module。
    """
    entry = reg.get(dep, {})
    parent = entry.get("required_by", "")
    if parent and dep in _parent_vendor_crates(sd, parent):
        return True
    if get_lang(sd, dep) in VENDOR_LANGS and _is_pure_lib_crate(sd, dep):
        return True
    return False


MAX_DEP_DEPTH = 5

# 单包修复轮数上限：按 pkgs/<pkg>/fix_state.json 的 fix_round 显式计数
# （supervisor 每次路由 fixer——fix 或 resubmit 模式——时 +1，regenerate 时清零）。
# 超过上限强制 abort，防止 rebuild 死循环（此前没有任何上限）。
MAX_FIX_ROUNDS = 8

# 连续"修复无产出"轮数上限：fixer 退出但既未重新提交构建、也未注册新依赖、也未 abort。
MAX_NO_OUTPUT_ROUNDS = 2

# verify_install 重返上限：ci_check_result.json 写入路径异常时 supervisor 会反复重跑 CI，
# 超过上限直接 fail，把"路径类 bug"从死循环降级为快速失败。
MAX_CI_ATTEMPTS = 3


def _ci_runs_for_current_build(sd: Path) -> int:
    """统计本次构建完成后 verify_install 实际执行完成的次数。

    从 timeline.jsonl 派生：最近一次 build_main 结束（构建完成）之后的
    verify_install action.end 数量。不用计数器递增——determine_action 每轮
    会被 job_runner/agent 调用多次，在读路径递增会把重试预算烧在重复读取上
    （实际只跑了一两次 CI 就熔断）。按事件顺序单遍扫描，build_main 结束
    即重置计数，重建后自动从新构建算起。
    """
    timeline = sd / "timeline.jsonl"
    if not timeline.exists():
        return 0
    runs = 0
    for line in timeline.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "action.end":
            continue
        action = (ev.get("data") or {}).get("action")
        if action == "build_main":
            runs = 0
        elif action == "verify_install":
            runs += 1
    return runs


# dnf/CI 输出里的加载噪音行（repo 列表、下载进度、提示语），提取错误时需剔除
_CI_NOISE_RE = re.compile(
    r"(^Added .+ repo from |\d+(\.\d+)?\s*[kM]B/s\s*\||Last metadata expiration"
    r"|^\(try to add|^--skip-broken|^Error downloading|^\s*$)")


def _ci_error_essence(err: str, limit: int = 800) -> str:
    """从 CI/dnf 原始输出提取可操作的核心错误。

    dnf 输出结构：前面全是 repo 加载噪音，关键信息（Error:/Problem/
    nothing provides ...）在末尾。旧实现 str(e)[:200] 从头截断，用户只能
    看到 repo 镜像列表，完全无法判断失败原因。这里剔除噪音行后从
    Error/Problem 段截取；找不到标记段则取尾部；超长从尾部保留。
    """
    raw = str(err)
    lines = [l.rstrip() for l in raw.splitlines()]
    lines = [l for l in lines if not _CI_NOISE_RE.search(l)]
    if lines:
        start = next(
            (i for i, l in enumerate(lines)
             if re.match(r"\s*(Error|Problem|Problems)\b", l)
             or "conflicting requests" in l),
            None)
        text = "\n".join(lines[start:] if start is not None else lines[-8:])
    else:
        text = raw.strip()
    text = text.strip()
    if len(text) > limit:
        text = "…" + text[-limit:]
    return text


def _missing_requires_hint(raw: str) -> str:
    """从 dnf 'nothing provides X needed by Y' 提取缺失依赖，给出可行动指引。"""
    missing = sorted({m.group(1) for m in re.finditer(
        r"nothing provides ([^\s]+) needed by ([^\s]+)", raw)})
    if not missing:
        return ""
    return ("\n缺失运行时依赖: " + ", ".join(missing) +
            "（所有已配置软件源均未提供）。默认递归模式下重新提交本包会自动引入该依赖；"
            "也可先单独提交该依赖的引包任务，成功后再重新提交本包。")


def _derive_fail_reason(sd: Path, wf: dict, reg: dict, pkgname: str) -> str:
    """fail 时调用方只传了包名/空串（agent 未按约定传 reason）的兜底推导。

    依次取：主包 build_rpm_result 的 failure_reason → 最新 failure_analysis
    的 reason（agent 产出的人话根因，优于原始输出）→ ci_errors 的核心段
    （剔除 repo 噪音、保留 Error/Problem 段与缺失依赖指引）→ dep_registry
    里的 dep error。保证 workflow error（最终发给前端的失败原因）永远有
    实质内容且用户能看懂。
    """
    br_path = sd / f"pkgs/{pkgname}/build_rpm_result.json"
    br = read_json(br_path) if br_path.exists() else {}
    reason = (br.get("failure_reason") or "").strip()
    if reason:
        return reason
    analyses = sorted(
        sd.glob(f"pkgs/{pkgname}/failure_analysis_*.json"),
        key=lambda p: p.stat().st_mtime, reverse=True)
    for ap in analyses:
        try:
            reason = (read_json(ap).get("reason") or "").strip()
        except Exception:
            continue
        if reason:
            return reason
    ci_errors = br.get("ci_errors") or []
    if ci_errors:
        essence = "\n".join(_ci_error_essence(e) for e in ci_errors[:3])
        hint = _missing_requires_hint("\n".join(str(e) for e in ci_errors[:3]))
        return "CI 检查未通过:\n" + essence + hint
    for dep, entry in reg.items():
        err = (entry.get("error") or "").strip() if isinstance(entry, dict) else ""
        if err:
            return f"dep {dep}: {err}"
    return "unknown failure"


# ── fix_state.json：修复链路计数器的唯一事实来源 ─────────────────────────────
# 计数器不能放 build_rpm_result.json（copr_client 提交、pkg-builder 自检失败都会
# 整文件覆写，计数会被静默清零），也不能用 glob failure_analysis 文件数
# （precheck 预写污染、regenerate 后不重置）。fix_state.json 只有本脚本读写
# （另有 job_runner 仅对 mismatch_count 做 read-modify-write），无覆写风险。

def _fix_state_path(sd: Path, pkgname: str) -> Path:
    return sd / "pkgs" / pkgname / "fix_state.json"


def _read_fix_state(sd: Path, pkgname: str, chroot: str | None = None) -> dict:
    """读取修复计数器（fix_round / no_output_rounds / mismatch_count）。

    多 chroot（§8.2）：计数器按 (pkg, chroot) 计，存 fix_state.json 的
    "chroots": {<chroot>: {...}} 子字典；chroot 指定时返回该 chroot 的扁平视图
    （per-chroot 键缺失时回落包级旧值——兼容读旧位置）；chroot=None 返回包级视图
    （含 "chroots" 子字典原样保留，供 read-modify-write 不丢数据）。

    兼容旧位置：build_rpm_result.no_output_rounds、dep_registry[pkg].no_output_rounds
    仅作 fallback 读取，不再写入。
    """
    state: dict = {}
    result_path = sd / f"pkgs/{pkgname}/build_rpm_result.json"
    if result_path.exists():
        try:
            br = read_json(result_path)
            if "no_output_rounds" in br:
                state["no_output_rounds"] = br["no_output_rounds"]
        except Exception:
            pass
    reg_path = sd / "dep_registry.json"
    if reg_path.exists():
        try:
            entry = read_json(reg_path).get(pkgname, {})
            if isinstance(entry, dict) and "no_output_rounds" in entry:
                state["no_output_rounds"] = entry["no_output_rounds"]
        except Exception:
            pass
    p = _fix_state_path(sd, pkgname)
    if p.exists():
        try:
            state.update(read_json(p))
        except Exception:
            pass
    if chroot is None:
        return state
    # per-chroot 扁平视图：包级旧值打底，chroots[chroot] 覆盖
    merged = {k: v for k, v in state.items() if k != "chroots"}
    subs = state.get("chroots")
    if isinstance(subs, dict):
        sub = subs.get(chroot)
        if isinstance(sub, dict):
            merged.update(sub)
    return merged


def _write_fix_state(sd: Path, pkgname: str, state: dict) -> None:
    p = _fix_state_path(sd, pkgname)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_json(p, state)


def _fix_rounds(sd: Path, pkgname: str, chroot: str | None = None) -> int:
    """该包（指定 chroot）已经历的修复轮数（显式计数，见 _bump_fix_round）。"""
    return int(_read_fix_state(sd, pkgname, chroot).get("fix_round", 0) or 0)


def _bump_fix_round(sd: Path, pkgname: str, chroot: str | None = None) -> int:
    """supervisor 每次路由 fixer（fix 或 resubmit 模式）时计一轮，返回新轮数。
    chroot 指定时计 (pkg, chroot) 维度（per-chroot 缺失时从包级旧值起计）。"""
    state = _read_fix_state(sd, pkgname)
    if chroot:
        subs = state.setdefault("chroots", {})
        sub = subs.setdefault(chroot, {})
        sub["fix_round"] = int(sub.get("fix_round", state.get("fix_round", 0)) or 0) + 1
        _write_fix_state(sd, pkgname, state)
        return sub["fix_round"]
    state["fix_round"] = int(state.get("fix_round", 0) or 0) + 1
    _write_fix_state(sd, pkgname, state)
    return state["fix_round"]


def _set_fix_counter(sd: Path, pkgname: str, key: str, value, chroot: str | None = None) -> None:
    """写单个计数器（no_output_rounds 自增回写等场景），chroot 指定时写 (pkg, chroot) 维度。"""
    state = _read_fix_state(sd, pkgname)
    if chroot:
        state.setdefault("chroots", {}).setdefault(chroot, {})[key] = value
    else:
        state[key] = value
    _write_fix_state(sd, pkgname, state)


def _clear_fix_counters(sd: Path, pkgname: str, *keys: str, chroot: str | None = None) -> None:
    """清零指定计数器：regenerate 清 fix_round/no_output_rounds；确认重交后清 no_output_rounds。
    chroot 指定时只清该 chroot 的计数；否则清包级顶层（"chroots" 子字典保留）。"""
    p = _fix_state_path(sd, pkgname)
    state = _read_fix_state(sd, pkgname)
    if chroot:
        subs = state.get("chroots")
        sub = subs.get(chroot) if isinstance(subs, dict) else None
        if isinstance(sub, dict):
            for k in keys:
                sub.pop(k, None)
    else:
        for k in keys:
            state.pop(k, None)
    if state or p.exists():
        _write_fix_state(sd, pkgname, state)


def _current_build_id(sd: Path, pkgname: str, reg: dict | None = None, chroot: str | None = None):
    """该包最近一次 COPR build_id。

    chroot 指定时按 per-chroot 取：dep_registry chroots[c].build_id 优先，
    build_rpm_result.copr_build_ids 兜底，旧包级字段再兜底；
    不传 chroot 时行为与旧版完全一致（build_rpm_result 优先，dep 兜底 dep_registry）。
    """
    if chroot and reg and isinstance(reg.get(pkgname), dict):
        ch = reg[pkgname].get("chroots")
        if isinstance(ch, dict) and isinstance(ch.get(chroot), dict):
            bid = ch[chroot].get("build_id")
            if bid:
                return bid
    result_path = sd / f"pkgs/{pkgname}/build_rpm_result.json"
    if result_path.exists():
        try:
            br = read_json(result_path)
            if chroot:
                bids = br.get("copr_build_ids")
                if isinstance(bids, dict) and bids.get(chroot):
                    return bids[chroot]
            bid = br.get("copr_build_id")
            if bid:
                return bid
        except Exception:
            pass
    if reg and isinstance(reg.get(pkgname), dict):
        return reg[pkgname].get("copr_build_id")
    return None


def fix_context(sd: Path, pkgname: str, old_build_id, trigger: str | None = None,
                chroot: str | None = None) -> dict:
    """生成 pkg-fixer 的上下文参数：mode / trigger / 修复轮数 / 无产出计数 / analysis_file 精确路径。

    trigger 由调用方显式指定；缺省时按 build_rpm_result.status 推断
    （ci_failed → ci_failed，否则 build_failed）。resubmit 入口由 supervisor 直接
    指定 trigger="resubmit"，resubmit 轮同样计入 fix_round 熔断。

    chroot 指定时（多 chroot 按失败 chroot 派发，§8.2）：计数器取 (pkg, chroot)
    维度，analysis_file 用 chroot 专属文件名（避免共享 build_id 时多个失败 chroot
    互相覆盖 analysis），ctx 带 "chroot" 字段告知 fixer/analyzer 本轮目标 chroot。
    """
    result_path = sd / f"pkgs/{pkgname}/build_rpm_result.json"
    status = ""
    ci_errors: list = []
    if result_path.exists():
        try:
            result = read_json(result_path)
            status = result.get("status", "")
            ci_errors = result.get("ci_errors", []) or []
        except Exception:
            pass
    state = _read_fix_state(sd, pkgname, chroot)
    if trigger is None:
        trigger = "ci_failed" if status == "ci_failed" else "build_failed"
    if old_build_id and chroot:
        analysis_file = f"pkgs/{pkgname}/failure_analysis_{pkgname}_{old_build_id}_{chroot}.json"
    elif old_build_id:
        analysis_file = f"pkgs/{pkgname}/failure_analysis_{pkgname}_{old_build_id}.json"
    elif chroot:
        analysis_file = f"pkgs/{pkgname}/failure_analysis_{pkgname}_{chroot}.json"
    else:
        analysis_file = f"pkgs/{pkgname}/failure_analysis_{pkgname}.json"
    ctx: dict = {
        "mode": "resubmit" if trigger == "resubmit" else "fix",
        "trigger": trigger,
        "round": int(state.get("fix_round", 0) or 0),
        "max_rounds": MAX_FIX_ROUNDS,
        "no_output": int(state.get("no_output_rounds", 0) or 0),
        "max_no_output": MAX_NO_OUTPUT_ROUNDS,
        "analysis_file": analysis_file,
    }
    if chroot:
        ctx["chroot"] = chroot
    if state.get("mismatch_count"):
        ctx["mismatch_count"] = state["mismatch_count"]
    if ci_errors:
        ctx["ci_errors"] = ci_errors
    # precheck 的高置信修复线索（仅 hint，fixer 可推翻）
    hint = f"pkgs/{pkgname}/failure_hint_{pkgname}_{old_build_id}.json" if old_build_id \
        else f"pkgs/{pkgname}/failure_hint_{pkgname}.json"
    if (sd / hint).exists():
        ctx["hint_file"] = hint
    return ctx


def _emit_fix_action(sd: Path, action: str, pkgname: str, old_build_id,
                     trigger: str | None = None, chroot: str | None = None) -> tuple[str, str, int]:
    """计一轮修复，写 pkgs/<pkg>/fix_context.json 后返回 fix_failure/fix_failure_dep action。
    chroot 指定时计数与上下文均按 (pkg, chroot) 维度（§8.2），并把失败 chroot
    记入 _DISPATCH_EXTRA 供 main() 输出 CHROOT 键。"""
    _bump_fix_round(sd, pkgname, chroot)
    ctx = fix_context(sd, pkgname, old_build_id, trigger=trigger, chroot=chroot)
    pkg_dir = sd / "pkgs" / pkgname
    pkg_dir.mkdir(parents=True, exist_ok=True)
    write_json(pkg_dir / "fix_context.json", ctx)
    if chroot:
        _DISPATCH_EXTRA["chroot"] = chroot
    return (action, pkgname, 0)


def _resubmitted(result: dict | None, old_build_id) -> bool:
    """判断 fixer 是否已重新提交构建。

    单一事实来源：submit_fix.py 提交成功后写入的 resubmitted: true
    （旧启发式 status==copr_running 且 new_id != old_id 已废弃——copr_client
    异步返回同 id 时会误判为未重交）。
    """
    if not result or not result.get("resubmitted"):
        return False
    new_id = result.get("copr_build_id")
    return bool(new_id) and str(new_id) != str(old_build_id)


def compute_depth(dep_name: str, reg: dict, pkgname: str, _seen: frozenset = frozenset()) -> int:
    """从 required_by 链计算依赖深度。主包 = 0，直接依赖 = 1，以此类推。
    找不到上级时返回 1（降级处理，不阻断主流程）。"""
    entry = reg.get(dep_name, {})
    parent = entry.get("required_by", "")
    if not parent or parent == pkgname:
        return 1
    if parent not in reg:
        return 1  # 上级不在 registry，降级为直接依赖
    if parent in _seen:
        return 99  # 循环依赖保护：parent 已在访问链上
    return 1 + compute_depth(parent, reg, pkgname, _seen | {dep_name})


def _record_built_pkg(sd: Path, wf: dict, pkg: str) -> None:
    """异步轮询路径回填 built_pkgs（与 update_after_build 同步路径行为一致）。"""
    wf.setdefault("built_pkgs", [])
    if pkg not in wf["built_pkgs"]:
        wf["built_pkgs"].append(pkg)
        wf_files = list(sd.glob("workflow_*.json"))
        if wf_files:
            write_json(wf_files[0], wf)


def _skip_dep_chroot(sd: Path, wf: dict, reg: dict, reg_path: Path,
                     dep: str, chroot: str, targets: list[str], reason: str):
    """dep 单 chroot 超限/放弃 → 该 chroot 置 skipped 并重算包级聚合（§8.2）。

    其余失败 chroot 留给重新求值后的 Priority 3 继续处理；全部目标 chroot 均
    skipped 时聚合为 build_failed 且无 failed chroot，由 Priority 3 入口守卫 fail。
    """
    entry = reg[dep]
    ch = entry.setdefault("chroots", {})
    cur = ch.get(chroot)
    ch[chroot] = {"status": "skipped",
                  "build_id": cur.get("build_id") if isinstance(cur, dict) else None}
    entry["status"] = _refresh_pkg_status(entry, targets)
    if entry["status"] == "build_failed":
        entry["error"] = reason
    else:
        entry.pop("error", None)  # 部分成功 + 部分 skipped 聚合为成功，不残留 error
    write_json(reg_path, reg)
    print(f"[supervisor] {dep} chroot {chroot} 置 skipped：{reason}", file=sys.stderr)
    return determine_action(sd, wf, reg)


def _skip_main_chroot(sd: Path, wf: dict, reg: dict, main_result: dict,
                      main_result_path: Path, pkgname: str, chroot: str,
                      targets: list[str], reason: str):
    """主包单 chroot 超限/放弃 → chroot_status 置 skipped（§8.2）。

    全部 chroot 终态且有成功 → 主包 success（部分成功 + 部分 skipped 聚合为成功，
    skipped 由报告标注）；全部 skipped → fail；否则保持 failed 继续处理其余失败 chroot。
    """
    ch = main_result.setdefault("chroot_status", {})
    cur = ch.get(chroot)
    ch[chroot] = {"status": "skipped",
                  "build_id": cur.get("build_id") if isinstance(cur, dict) else None}
    states = [ch[c].get("status", "pending") if isinstance(ch.get(c), dict) else "pending"
              for c in targets]
    if states and all(s in _CHROOT_CLOSED_STATUSES for s in states):
        if any(s in ("build_done", "reused") for s in states):
            main_result["status"] = "success"
        else:
            write_json(main_result_path, main_result)
            return ("fail", f"{pkgname} 全部目标 chroot 均被跳过: {reason}", None)
    write_json(main_result_path, main_result)
    print(f"[supervisor] 主包 chroot {chroot} 置 skipped：{reason}", file=sys.stderr)
    return determine_action(sd, wf, reg)


def _resolve_analysis_file(sd: Path, pkgname: str, build_id, chroot: str | None):
    """定位本轮 failure_analysis 文件：chroot 专属名优先，旧命名兜底（兼容旧 analyzer）。

    返回 (path, is_legacy_shared)。is_legacy_shared=True 表示命中不带 chroot 的共享
    文件名——多 chroot 共享 build_id 时消费后需删除，避免下一个失败 chroot 误用
    同一份分析。文件不存在时返回 chroot 专属路径（供调用方派发 fix 后写入）。
    """
    if chroot:
        specific = sd / f"pkgs/{pkgname}/failure_analysis_{pkgname}_{build_id}_{chroot}.json" if build_id \
            else sd / f"pkgs/{pkgname}/failure_analysis_{pkgname}_{chroot}.json"
        if specific.exists():
            return specific, False
        legacy = sd / f"pkgs/{pkgname}/failure_analysis_{pkgname}_{build_id}.json" if build_id \
            else sd / f"pkgs/{pkgname}/failure_analysis_{pkgname}.json"
        if legacy.exists():
            try:
                data = read_json(legacy)
            except Exception:
                data = {}
            # analyze 输出扩展的 chroot 字段（§8.2）：标注了其他 chroot 的视为未找到
            if data.get("chroot") in (None, "", chroot):
                return legacy, True
        return specific, False
    analysis_file = sd / f"pkgs/{pkgname}/failure_analysis_{pkgname}_{build_id}.json" if build_id \
        else sd / f"pkgs/{pkgname}/failure_analysis_{pkgname}.json"
    # 兜底：agent 在 build_id 为空时可能写成 failure_analysis_{pkg}_.json（尾部多下划线）
    if not analysis_file.exists():
        fallback = sd / f"pkgs/{pkgname}/failure_analysis_{pkgname}_.json"
        if fallback.exists():
            analysis_file = fallback
    return analysis_file, False


def _sync_dep_result_failed(sd: Path, dep: str, reason: str) -> None:
    """轮询确认构建失败后同步 dep 的 build_rpm_result（copr_running → failed）。"""
    dep_result_path = sd / f"pkgs/{dep}/build_rpm_result.json"
    if dep_result_path.exists():
        try:
            br = read_json(dep_result_path)
            if br.get("status") == "copr_running":
                br["status"] = "failed"
                br["failure_reason"] = reason
                write_json(dep_result_path, br)
        except Exception:
            pass


def _refine_failed_chroots(sd: Path, ch_map: dict) -> None:
    """同步路径构建失败后按 chroot 细化成败：能轮询到逐 chroot 结果就按实际标记
    （多 chroot 一次提交可能只有部分 chroot 失败），轮询不到则保持 failed（保守，
    等价旧行为）。仅调整本轮已被标记为 failed 的 chroot。"""
    poll_cache: dict = {}
    for c, cinfo in ch_map.items():
        if not isinstance(cinfo, dict) or cinfo.get("status") != "failed":
            continue
        bid = cinfo.get("build_id")
        if not bid:
            continue
        if bid not in poll_cache:
            poll_cache[bid] = _poll_copr_build_chroots(bid, sd)
        cmap = poll_cache[bid]
        if cmap and cmap.get(c) == "succeeded":
            cinfo["status"] = "build_done"


def _promote_pending_deps(reg: dict, tracking: bool, targets: list[str]) -> bool:
    """pending_deps 晋升：前置依赖就绪后升回 evaluate_done（§8.1 改为按 chroot 判定）。

    多 chroot：先做 skip 级联（_apply_doomed_chroots），再按可提交集合 S(B) 非空
    判定——任一 chroot 可提交即晋升，S 之外的 chroot 挂 pending 等增量补交；
    旧 session：包级 blockers 检查（行为不变）。返回是否有改动。
    """
    promoted = False
    for dep_name, dep_info in list(reg.items()):
        if dep_info["status"] != DEP_WAITING_STATUS:
            continue
        if tracking:
            if _apply_doomed_chroots(reg, dep_name, targets):
                promoted = True
            if reg[dep_name]["status"] != DEP_WAITING_STATUS:
                continue  # 级联已把它带出 pending_deps（build_failed/evaluate_done）
            if _submittable_chroots(reg, dep_name, targets):
                reg[dep_name]["status"] = "evaluate_done"
                promoted = True
        else:
            blockers = [k for k, v in reg.items()
                        if v.get("required_by") == dep_name
                        and v["status"] not in DEP_READY_STATUSES]
            if not blockers:
                reg[dep_name]["status"] = "evaluate_done"
                promoted = True
    return promoted


def _dispatch_dep_build(sd: Path, wf: dict, reg: dict, tracking: bool, targets: list[str]):
    """pending_build（evaluate_done）→ build_dep 派发；无可派发返回 None。

    多 chroot：先 skip 级联，再算可提交子集 S(B)；S 为空（全部 chroot 前置未就绪）
    → 挂回 pending_deps 等下轮晋升；S 非空 → _DISPATCH_EXTRA["chroots"] = S，由 main()
    输出 COPR_BUILD_CHROOTS 键（构建脚本侧优先级 COPR_BUILD_CHROOTS > COPR_CHROOTS > COPR_CHROOT）。
    """
    PKGNAME = wf["pkgname"]
    reg_path = sd / "dep_registry.json"
    pending_build = [k for k, v in reg.items() if v["status"] == "evaluate_done"]
    if not pending_build:
        return None
    # vendor 拦截（第二层）：按 crate 身份判定（父包 crate 清单命中，或证据
    # 确认为纯库 crate/module）；ripgrep / pydantic-core 类包级依赖不拦截。
    crate_deps = [k for k in pending_build if _is_vendor_crate(sd, k, reg)]
    if crate_deps:
        for d in crate_deps:
            reg[d]["status"] = "vendor_only"
            print(f"[supervisor] {d} 判定为 crate/module，置 vendor_only（父包 vendor 解决）",
                  file=sys.stderr)
        write_json(reg_path, reg)
        return determine_action(sd, wf, reg)
    over_depth = [k for k in pending_build if compute_depth(k, reg, PKGNAME) > MAX_DEP_DEPTH]
    if over_depth:
        return ("fail", f"dep depth exceeded {MAX_DEP_DEPTH}: {over_depth}", None)
    dep = pending_build[0]
    if tracking:
        if _apply_doomed_chroots(reg, dep, targets):
            write_json(reg_path, reg)
            return determine_action(sd, wf, reg)
        S = _submittable_chroots(reg, dep, targets)
        if not S:
            # 全部目标 chroot 的前置依赖均未就绪 → 挂回 pending_deps 等待晋升
            reg[dep]["status"] = DEP_WAITING_STATUS
            write_json(reg_path, reg)
            return determine_action(sd, wf, reg)
        _DISPATCH_EXTRA["chroots"] = S
    lang = get_lang(sd, dep)
    return ("build_dep", dep, build_delay(lang))


# ── ROS 模式分流 ─────────────────────────────────────────────────────────────
def _is_ros_session(sd: Path) -> bool:
    """session 是否为 ROS 引包任务（前端 mode=ros → session.json import_type=ros）。"""
    s = _read_session(sd)
    return s.get("import_type") == "ros" or s.get("mode") == "ros"


def _ros_route(sd: Path, wf: dict, PKGNAME: str, reg: dict):
    """ROS 链优先级：ros_prep → ros_fetch → ros_spec → 复用通用 build/fix 链。

    返回 None 表示落到通用链继续判定（build_main/wait/fix_failure 等复用）。
    伪 gate_result 是分流支点：ros_prep 产出 decision∈(introduce_new, reuse_*)
    的伪 gate_result 后，通用链的 gate_valid 检查自然放行。
    """
    gate_path = sd / f"pkgs/{PKGNAME}/gate_result_{PKGNAME}.json"

    # 1. 伪 gate_result 未就绪 → ros_prep（定位 + 官方判定 + manifest + 伪 gate）
    if not gate_path.exists():
        return ("ros_prep", PKGNAME, 0)
    try:
        g = read_json(gate_path)
        if g.get("overall_status") != "done":
            return ("fail", g.get("error", "ROS 预检未完成"), None)
    except Exception:
        return ("fail", f"gate_result_{PKGNAME}.json 损坏", None)

    # 2. reuse 主包：goal_achieved 已由 ros_prep 代写 → 直接 done
    if wf.get("goal_achieved") is True:
        return ("done", PKGNAME, None)

    # 3. explicit 缺口：缺口清单存在 → 终止（闭环=用户把缺口包追加进列表重提）
    missing_path = sd / f"pkgs/{PKGNAME}/missing_deps_{PKGNAME}.txt"
    if missing_path.exists():
        missing = missing_path.read_text(encoding="utf-8", errors="ignore").split()
        if missing:
            return ("fail", "缺少官方源依赖，需先引入后重提: " + " ".join(missing), None)

    # 4. 源码未就绪 → ros_fetch（cache → sources/）
    src_dir = sd / "sources" / PKGNAME
    if not (src_dir.is_dir() and any(src_dir.iterdir())):
        return ("ros_fetch", PKGNAME, 0)

    # 5. spec 已就绪 → 通用链（build_main/resubmit/修复闭环）
    if (sd / f"pkgs/{PKGNAME}/{PKGNAME}.spec").exists():
        return None

    # 6. spec 未就绪且无构建结果 → ros_spec（生成 spec，后续通用链提交构建）
    main_result_path = sd / f"pkgs/{PKGNAME}/build_rpm_result.json"
    if not main_result_path.exists():
        return ("ros_spec", PKGNAME, 0)

    # 7. 其余（构建轮询/失败修复/verify/feedback/收尾）→ 通用链
    return None


def determine_action(sd: Path, wf: dict, reg: dict) -> tuple[str, str, int | None]:
    """返回 (action, target, delay_seconds)。delay=None 表示停止循环。"""
    PKGNAME = wf["pkgname"]

    # ── ROS 模式分流（全方案唯一的结构性侵入点）────────────────────────────
    if _is_ros_session(sd):
        ros_action = _ros_route(sd, wf, PKGNAME, reg)
        if ros_action is not None:
            return ros_action

    # 优先级 -1：evaluate_main 失败，等待 AI 分析
    if wf.get("evaluate_failed"):
        analysis_file = sd / f"pkgs/{PKGNAME}/evaluate_analysis_{PKGNAME}.json"
        if not analysis_file.exists():
            return ("analyze_evaluate_main", PKGNAME, 0)
        data = read_json(analysis_file)
        verdict = data.get("verdict", "abort")
        if verdict == "retry":
            wf.pop("evaluate_failed", None)
            wf_files = list(sd.glob("workflow_*.json"))
            if wf_files:
                write_json(wf_files[0], wf)
            (sd / f"pkgs/{PKGNAME}/gate_result_{PKGNAME}.json").unlink(missing_ok=True)
            # suggestion 留给重试的 pkg-evaluator（它读完后自行删除）
            suggestion = data.get("suggestion", "")
            if suggestion:
                (sd / f"pkgs/{PKGNAME}/evaluate_retry_hint.txt").write_text(suggestion, encoding="utf-8")
            analysis_file.unlink(missing_ok=True)
            return ("evaluate_main", PKGNAME, 60)
        return ("fail", data.get("reason", wf.get("evaluate_failed", "evaluate failed")), None)

    # 优先级 0：主包 gate_result 不存在或内容无效 → evaluate_main
    gate_path = sd / f"pkgs/{PKGNAME}/gate_result_{PKGNAME}.json"
    gate_valid = False
    if gate_path.exists():
        try:
            g = read_json(gate_path)
            decision = g.get("result", {}).get("decision", "")
            if g.get("overall_status") == "done" and \
               decision in ("introduce_new", "introduce_new_with_ref",
                            "reuse_official", "reuse_copr_project",
                            "reuse_additional_repo",
                            "reuse_eur_srpm", "evaluate", "upgrade_user_repo"):
                gate_valid = True
            elif decision == "check_failed":
                # 网络/下载临时失败，删除后重试（不算损坏，delay 长一些）
                gate_path.unlink(missing_ok=True)
                return ("evaluate_main", PKGNAME, 120)
        except Exception:
            pass
    if not gate_valid:
        if gate_path.exists():
            gate_path.unlink(missing_ok=True)  # 损坏则删除，下次重跑
        return ("evaluate_main", PKGNAME, 60)

    # gate_result 已确认 reuse → 直接 done（goal_achieved 由 update 写入）
    if wf.get("goal_achieved") is True:
        return ("done", PKGNAME, None)

    # 读主包 build_rpm_result
    main_result_path = sd / f"pkgs/{PKGNAME}/build_rpm_result.json"
    main_result = None
    if main_result_path.exists():
        try:
            main_result = read_json(main_result_path)
        except Exception:
            pass
    main_status = main_result.get("status") if main_result else None
    if main_status and main_status not in VALID_BUILD_STATUSES:
        main_status = None
        main_result = None

    # build_rpm_result 为空/无效时，标记为 interrupted 触发重建
    if main_status is None and main_result_path.exists():
        try:
            raw = read_json(main_result_path)
            if raw.get("status") not in VALID_BUILD_STATUSES:
                raw["status"] = "interrupted"
                write_json(main_result_path, raw)
        except Exception:
            pass

    # 优先级 1a：有 dep evaluate 失败，等待 AI 分析
    failed_eval_deps = [k for k, v in reg.items() if v["status"] == "evaluate_failed"]
    if failed_eval_deps:
        dep = failed_eval_deps[0]
        analysis_file = sd / f"pkgs/{dep}/evaluate_analysis_{dep}.json"
        if not analysis_file.exists():
            return ("analyze_evaluate", dep, 0)
        data = read_json(analysis_file)
        verdict = data.get("verdict", "abort")
        if verdict == "retry":
            reg[dep]["status"] = "pending_evaluate"
            reg[dep].pop("error", None)
            write_json(sd / "dep_registry.json", reg)
            # suggestion 留给重试的 pkg-evaluator（它读完后自行删除）
            suggestion = data.get("suggestion", "")
            if suggestion:
                (sd / f"pkgs/{dep}/evaluate_retry_hint.txt").write_text(suggestion, encoding="utf-8")
            analysis_file.unlink(missing_ok=True)
            (sd / f"pkgs/{dep}/gate_result_{dep}.json").unlink(missing_ok=True)
            return ("evaluate", dep, 60)
        reason = data.get("reason", reg[dep].get("error", f"dep {dep} evaluate failed"))
        return ("fail", reason, None)

    # 优先级 1b：有 dep 待 evaluate
    pending_eval = [k for k, v in reg.items() if v["status"] == "pending_evaluate"]
    if pending_eval:
        # vendor 拦截（第一层）：注册时带 lang 的 crate/module 不进 evaluate/build，
        # 直接置 vendor_only 终态——可用性由父包 vendor 保证，无 RPM 产物。
        vendor_eval = [k for k in pending_eval
                       if str(reg[k].get("lang", "")).lower() in VENDOR_LANGS]
        if vendor_eval:
            for d in vendor_eval:
                reg[d]["status"] = "vendor_only"
                print(f"[supervisor] {d} lang ∈ {sorted(VENDOR_LANGS)}，置 vendor_only（父包 vendor 解决）",
                      file=sys.stderr)
            write_json(sd / "dep_registry.json", reg)
            return determine_action(sd, wf, reg)
        # ROS 依赖拦截（deep 模式）：ros_prep 已完成官方源判定并注册，
        # 无 URL、不走普通 gate——注册即视为已评估，直接 evaluate_done 进构建链。
        ros_eval = [k for k in pending_eval
                    if str(reg[k].get("lang", "")).lower() == "ros"]
        if ros_eval:
            for d in ros_eval:
                reg[d]["status"] = "evaluate_done"
                # 补伪 gate_result（lang/version 供 pkg-builder 读取，版本由
                # analyze_ros_deps 的 package.xml 解析兜底）
                dep_gate = sd / f"pkgs/{d}/gate_result_{d}.json"
                if not dep_gate.exists():
                    write_json(dep_gate, {
                        "pkgname": d, "lang": "ros", "version": "",
                        "overall_status": "done",
                        "result": {"lang": "ros", "version": "",
                                   "decision": "introduce_new",
                                   "reason": "ROS 依赖由 ros_prep 注册，评估在预检完成"},
                    })
                print(f"[supervisor] {d} lang=ros，跳过普通 evaluate（已在 ros_prep 判定）",
                      file=sys.stderr)
            write_json(sd / "dep_registry.json", reg)
            return determine_action(sd, wf, reg)
        dep = pending_eval[0]
        # 上游 URL 未填写时，优先用脚本直查（npm/PyPI/crates.io API），
        # 脚本查不到再走 resolve_upstream AI agent 兜底。
        if not reg[dep].get("url", ""):
            if reg[dep].get("url_error", ""):
                return ("fail", f"无法解析 {dep} 上游地址: {reg[dep]['url_error']}", None)
            # 尝试脚本解析
            try:
                rc = subprocess.run(
                    [sys.executable, str(_RESOLVE_SCRIPT),
                     "--pkg", dep, "--session-dir", str(sd)],
                    capture_output=True, text=True, timeout=30,
                )
                if rc.returncode == 0:
                    # 脚本成功写入 URL → 重新读 dep_registry 后继续
                    reg = read_json(sd / "dep_registry.json")
                    if reg[dep].get("url", ""):
                        pass  # url 已填充，进入下面的 evaluate 分支
                    else:
                        return ("resolve_upstream", dep, 0)
                else:
                    return ("resolve_upstream", dep, 0)
            except (subprocess.TimeoutExpired, OSError):
                return ("resolve_upstream", dep, 0)
        over_depth = [k for k in pending_eval if compute_depth(k, reg, PKGNAME) > MAX_DEP_DEPTH]
        if over_depth:
            return ("fail", f"dep depth exceeded {MAX_DEP_DEPTH}: {over_depth}", None)
        return ("evaluate", dep, 60)

    # 优先级 2：有 dep 待构建
    # pending_deps 状态：该 dep 曾返回 dep_needed，等待其前置依赖就绪后再重试
    # 若其前置依赖（required_by 链上新增的 dep）已就绪，则升回 evaluate_done
    # （多 chroot 下按 chroot 判定：任一 chroot 可提交即晋升，§8.1 提交门控）
    reg_path_local = sd / "dep_registry.json"
    tracking = _chroot_tracking(sd)
    targets = _target_chroots(sd) if tracking else []
    if _promote_pending_deps(reg, tracking, targets):
        write_json(reg_path_local, reg)

    action_dep = _dispatch_dep_build(sd, wf, reg, tracking, targets)
    if action_dep:
        return action_dep

    # 优先级 2.5：有 dep 处于 copr_running，全量轮询所有，更新 dep_registry 状态
    # 注意：_finalize_copr_build（拉日志）不在这里调用，由 job_runner wait loop 负责
    copr_running_deps = [k for k, v in reg.items() if v["status"] == "copr_running"]
    if copr_running_deps:
        changed = False
        still_running = []
        for dep in copr_running_deps:
            entry = reg[dep]
            chroots = entry.get("chroots") if isinstance(entry.get("chroots"), dict) else None
            if not (tracking and chroots):
                # 旧路径（单 chroot / 无 per-chroot 记账）：单 build_id 单状态，行为不变
                build_id = entry.get("copr_build_id")
                if not build_id:
                    entry["status"] = "evaluate_done"
                    changed = True
                    continue
                copr_state = _poll_copr_build(build_id, sd)
                if copr_state == "succeeded":
                    entry["status"] = "build_done"
                    _record_built_pkg(sd, wf, dep)
                    changed = True
                elif copr_state == "failed":
                    entry["status"] = "build_failed"
                    entry["error"] = f"copr build {build_id} failed"
                    _sync_dep_result_failed(sd, dep, f"copr build {build_id} failed")
                    changed = True
                else:
                    still_running.append(dep)
                continue
            # 多 chroot（§3.3 第 4 项）：逐 chroot 取各自最近一次提交的 build 轮询，
            # 按 chroot 更新 chroots[c] 并重算包级聚合 status
            poll_cache: dict = {}
            for c, cinfo in chroots.items():
                if not isinstance(cinfo, dict) or cinfo.get("status") != "building":
                    continue
                bid = cinfo.get("build_id") or entry.get("copr_build_id")
                if not bid:
                    # 无 build_id 的 building 记账是残缺的 → 回到 pending 由门控补交
                    cinfo["status"] = "pending"
                    changed = True
                    continue
                if bid not in poll_cache:
                    poll_cache[bid] = _poll_copr_build_chroots(bid, sd)
                cmap = poll_cache[bid]
                if cmap is None:
                    continue  # API 异常，保持 building 下轮再试
                cstate = cmap.get(c)
                if cstate == "succeeded":
                    cinfo["status"] = "build_done"
                    changed = True
                elif cstate == "failed":
                    cinfo["status"] = "failed"
                    changed = True
                # running / 该 chroot 不在此 build 的结果里 → 保持 building
            new_status = _refresh_pkg_status(entry, targets)
            if new_status == "copr_running":
                still_running.append(dep)
            else:
                entry["status"] = new_status
                if new_status == "build_done":
                    entry.pop("error", None)
                    _record_built_pkg(sd, wf, dep)
                elif new_status == "build_failed":
                    failed_cs = [c for c in targets
                                 if isinstance(chroots.get(c), dict) and chroots[c].get("status") == "failed"]
                    entry["error"] = f"copr build failed: chroot {','.join(failed_cs)}"
                    _sync_dep_result_failed(sd, dep, entry["error"])
                # evaluate_done：部分 chroot 未提交，回到 Priority 2 门控补交
                changed = True
        if changed:
            write_json(reg_path_local, reg)
        if still_running:
            return ("wait", f"{','.join(still_running)}(copr_running)", 60)
        # 所有 dep 状态已更新，重新检查 pending_deps 是否可晋升（2.5 结束后补跑一次 Priority 2 逻辑）
        if _promote_pending_deps(reg, tracking, targets):
            write_json(reg_path_local, reg)
        action_dep = _dispatch_dep_build(sd, wf, reg, tracking, targets)
        if action_dep:
            return action_dep

    # 优先级 3：有 dep 构建失败
    failed_deps = [k for k, v in reg.items() if v["status"] == "build_failed"]
    if failed_deps:
        dep = failed_deps[0]
        entry = reg[dep]
        # 多 chroot：按失败 chroot 派发 analyze（§8.2），fc = 本轮处理的失败 chroot（None=旧路径）
        chroots = entry.get("chroots") if isinstance(entry.get("chroots"), dict) else None
        per_chroot = tracking and bool(chroots)
        failed_chroots = [c for c in targets
                          if isinstance(chroots.get(c), dict) and chroots[c].get("status") == "failed"] \
            if per_chroot else []
        if per_chroot and not failed_chroots:
            # 全部 chroot 均已 skipped（超限/skip_chroot/级联），无可分析对象 → 包级失败
            return ("fail", entry.get("error", f"dep {dep} 全部目标 chroot 均被跳过"), None)
        fc = failed_chroots[0] if failed_chroots else None
        # 失败构建的 id 以 dep_registry 为准：fixer 重交后 build_rpm_result 已是新 id，
        # 若从 br 读，_resubmitted 的新旧 id 比较会失效（永远相等）；
        # 多 chroot 下取该 chroot 最近一次提交的 build（增量重交后各 chroot build_id 不同）
        dep_build_id = _current_build_id(sd, dep, reg, chroot=fc) if fc else entry.get("copr_build_id")
        dep_result_path = sd / f"pkgs/{dep}/build_rpm_result.json"
        dep_result = read_json(dep_result_path) if dep_result_path.exists() else None

        # builder 自检失败（从未提交 COPR，reg 与 br 都无 build_id）：fixer 无任何可诊断输入，
        # 回 builder 重建（SUBMODE=builder），重试 MAX_NO_OUTPUT_ROUNDS 次后 fail
        if not dep_build_id and not _current_build_id(sd, dep):
            if _fix_rounds(sd, dep) >= MAX_NO_OUTPUT_ROUNDS:
                return ("fail", f"dep {dep} builder 自检连续失败（未提交 COPR），已达重试上限", None)
            _bump_fix_round(sd, dep)
            entry["status"] = "evaluate_done"
            write_json(reg_path_local, reg)
            return ("build_dep", dep, build_delay(get_lang(sd, dep)))

        # MISMATCH 二次：job_runner 已在 fix_state.mismatch_count 计数，重生成一次仍
        # mismatch 说明根因不在 spec 文本。多 chroot 按 (pkg, chroot) 计，
        # 超限仅放弃该 chroot（§8.2），旧路径维持直接 fail
        if dep_result and "Package name mismatch" in dep_result.get("failure_reason", ""):
            mc = int(_read_fix_state(sd, dep, fc).get("mismatch_count", 0) or 0)
            if mc >= 2:
                if fc:
                    return _skip_dep_chroot(sd, wf, reg, reg_path_local, dep, fc, targets,
                                            f"第 {mc} 次 Package name mismatch，该 chroot 强制跳过")
                return ("fail", f"dep {dep} 第 {mc} 次 Package name mismatch，强制 abort", None)

        analysis_file, legacy_shared = _resolve_analysis_file(sd, dep, dep_build_id, fc)
        if not analysis_file.exists():
            return _emit_fix_action(sd, "fix_failure_dep", dep, dep_build_id, chroot=fc)
        analysis_data = read_json(analysis_file)
        verdict = analysis_data.get("verdict", "abort")
        if legacy_shared:
            # 多 chroot 共享 build_id 时旧命名 analysis 只消费一次，避免下一失败 chroot 误用
            analysis_file.unlink(missing_ok=True)
        if verdict == "skip_chroot" and fc:
            # analyzer 判定该 chroot 架构性不可构建 → 放弃该 chroot 而不是拖垮整个 job（§8.2）
            return _skip_dep_chroot(sd, wf, reg, reg_path_local, dep, fc, targets,
                                    analysis_data.get("reason", f"analyzer 判定 chroot {fc} 不可构建"))
        if verdict == "regenerate":
            # fixer 已删除 spec，回到 build_dep 由 pkg-builder 重新生成；
            # 清零修复计数（重新生成是重置事件；mismatch_count 保留以防重生成死循环）
            entry["status"] = "evaluate_done"
            entry.pop("no_output_rounds", None)  # legacy 字段清理
            entry.pop("chroots", None)  # 全量重生成：per-chroot 记账一并作废，各 chroot 重新提交
            _clear_fix_counters(sd, dep, "fix_round", "no_output_rounds")
            # per-chroot 计数同样清零（mismatch_count 各维度均保留）
            _raw = _read_fix_state(sd, dep)
            if isinstance(_raw.get("chroots"), dict):
                for _sub in _raw["chroots"].values():
                    if isinstance(_sub, dict):
                        _sub.pop("fix_round", None)
                        _sub.pop("no_output_rounds", None)
                _write_fix_state(sd, dep, _raw)
            write_json(reg_path_local, reg)
            return ("build_dep", dep, build_delay(get_lang(sd, dep)))
        if verdict in ("rebuild", "retry", "retry-transient", "retry-dep"):
            # fixer 已重新提交构建？（submit_fix 成功写入 resubmitted: true 且 build_id 更新）
            # 多 chroot：新 id 按 chroot 取（copr_build_ids 优先，包级 copr_build_id 兜底）
            new_id = None
            if fc:
                new_bids = dep_result.get("copr_build_ids") if isinstance(dep_result, dict) else None
                new_id = (new_bids.get(fc) if isinstance(new_bids, dict) else None) \
                    or (dep_result.get("copr_build_id") if dep_result else None)
                resubmitted = bool(dep_result and dep_result.get("resubmitted")
                                   and new_id and str(new_id) != str(dep_build_id))
            else:
                resubmitted = _resubmitted(dep_result, dep_build_id)
                if resubmitted:
                    new_id = dep_result["copr_build_id"]
            if resubmitted:
                entry["status"] = "copr_running"
                if fc:
                    # 增量重交：仅重交的失败 chroot 回到 building，其余 chroot 结果保留（§3.1）
                    chroots[fc] = {"status": "building", "build_id": new_id}
                entry["copr_build_id"] = dep_result["copr_build_id"]
                entry.pop("no_output_rounds", None)  # legacy 字段清理
                dep_result.pop("resubmitted", None)
                write_json(dep_result_path, dep_result)
                _clear_fix_counters(sd, dep, "no_output_rounds", chroot=fc)
                write_json(reg_path_local, reg)
                return determine_action(sd, wf, reg)
            # 检查 analyze 过程中是否有新增的未就绪前置依赖（required_by 指向当前 dep）。
            # 若存在，设为 pending_deps，等待前置依赖就绪后由 Priority 2 晋升逻辑自动升回 evaluate_done。
            # 这与 update_after_build 中 dep_needed 路径的行为一致。
            # 多 chroot：按 chroot 判定——任一 chroot 可提交即不挂起（§8.1）。
            if fc:
                has_blockers = not _submittable_chroots(reg, dep, targets)
            else:
                has_blockers = any(True for k, v in reg.items()
                                   if v.get("required_by") == dep
                                   and v["status"] not in DEP_READY_STATUSES)
            if has_blockers:
                entry["status"] = DEP_WAITING_STATUS
                write_json(reg_path_local, reg)
                # 重新评估：当前 dep 已变为 pending_deps，不再被 Priority 2 捕获，
                # Priority 3 将有机会处理 blocker 的 build_failed
                return determine_action(sd, wf, reg)
            # 修复轮数上限（多 chroot 按 (pkg, chroot) 计：单 chroot 超限仅放弃该 chroot，§8.2）
            if _fix_rounds(sd, dep, fc) >= MAX_FIX_ROUNDS:
                if fc:
                    return _skip_dep_chroot(sd, wf, reg, reg_path_local, dep, fc, targets,
                                            f"修复轮数达到上限 {MAX_FIX_ROUNDS}，该 chroot 强制跳过")
                return ("fail", f"dep {dep} 修复轮数达到上限 {MAX_FIX_ROUNDS}，强制 abort", None)
            # fixer 既未重新提交也未注册依赖 → 无产出计数，超限强制 abort
            state = _read_fix_state(sd, dep, fc)
            n = int(state.get("no_output_rounds", 0) or 0) + 1
            if n >= MAX_NO_OUTPUT_ROUNDS:
                if fc:
                    return _skip_dep_chroot(sd, wf, reg, reg_path_local, dep, fc, targets,
                                            f"连续 {n} 轮修复无产出，该 chroot 强制跳过")
                return ("fail", f"dep {dep} 连续 {n} 轮修复无产出，强制 abort", None)
            _set_fix_counter(sd, dep, "no_output_rounds", n, chroot=fc)
            return _emit_fix_action(sd, "fix_failure_dep", dep, dep_build_id, chroot=fc)
        reason = analysis_data.get("reason", f"dep {dep} build failed")
        if fc:
            reason = f"[{fc}] {reason}"
        return ("fail", reason, None)

    # 优先级 4：所有 dep 完成（或无 dep），处理主包
    # 多 chroot：按 chroot 判定——任一目标 chroot 的全部依赖就绪即可先提交该 chroot（§8.1）
    if tracking:
        all_deps_ready = (not reg) or any(
            all(_ready_for(v, c) for v in reg.values()) for c in targets)
    else:
        all_deps_ready = all(v["status"] in DEP_READY_STATUSES for v in reg.values())
    if all_deps_ready or not reg:
        # 主包 copr_running：轮询 COPR API，只更新本地状态文件
        if main_status == "copr_running":
            # fixer 重交已被确认（submit_fix 写入 resubmitted）→ 本轮修复有产出，
            # 消费标记并清零无产出计数
            if main_result and main_result.get("resubmitted"):
                main_result.pop("resubmitted", None)
                write_json(main_result_path, main_result)
                _clear_fix_counters(sd, PKGNAME, "no_output_rounds")
            build_id = main_result.get("copr_build_id") if main_result else None
            # 多 chroot：逐 chroot 轮询；chroot_status 记账缺失时从
            # copr_chroots/copr_build_ids 惰性建立（中断恢复兼容，§7 第 5 项）
            ch_status = main_result.get("chroot_status") if isinstance(main_result, dict) else None
            if tracking and isinstance(main_result, dict) and not isinstance(ch_status, dict):
                bids = main_result.get("copr_build_ids")
                submitted = main_result.get("copr_chroots")
                if isinstance(submitted, list) and submitted:
                    ch_status = {str(c): {"status": "building",
                                          "build_id": (bids.get(c) if isinstance(bids, dict) else None) or build_id}
                                 for c in submitted}
                    main_result["chroot_status"] = ch_status
            if tracking and isinstance(ch_status, dict) and ch_status:
                poll_cache: dict = {}
                for c, cinfo in ch_status.items():
                    if not isinstance(cinfo, dict) or cinfo.get("status") != "building":
                        continue
                    bid = cinfo.get("build_id") or build_id
                    if not bid:
                        cinfo["status"] = "pending"
                        continue
                    if bid not in poll_cache:
                        poll_cache[bid] = _poll_copr_build_chroots(bid, sd)
                    cmap = poll_cache[bid]
                    if cmap is None:
                        continue  # API 异常，保持 building 下轮再试
                    cstate = cmap.get(c)
                    if cstate == "succeeded":
                        cinfo["status"] = "build_done"
                    elif cstate == "failed":
                        cinfo["status"] = "failed"
                states = [ch_status[c].get("status", "pending") if isinstance(ch_status.get(c), dict)
                          else "pending" for c in targets]
                if states and all(s in _CHROOT_CLOSED_STATUSES for s in states):
                    # 全部 chroot 终态：有成功 → success（部分成功 + 部分 skipped 聚合成功）；
                    # 全 skipped → failed
                    if any(s in ("build_done", "reused") for s in states):
                        main_result["status"] = "success"
                        write_json(main_result_path, main_result)
                        _record_built_pkg(sd, wf, PKGNAME)
                        main_status = "success"
                    else:
                        main_result["status"] = "failed"
                        main_result["failure_reason"] = "全部目标 chroot 均被跳过"
                        write_json(main_result_path, main_result)
                        main_status = "failed"
                elif any(s == "failed" for s in states):
                    # 某 chroot 当前 build failed → 仅失败 chroot 进入 analyze，
                    # 已成功 chroot 结果保留（§3.3 第 4 项）
                    failed_cs = [c for c in targets
                                 if isinstance(ch_status.get(c), dict) and ch_status[c].get("status") == "failed"]
                    main_result["status"] = "failed"
                    main_result["failure_reason"] = f"copr build failed: chroot {','.join(failed_cs)}"
                    write_json(main_result_path, main_result)
                    main_status = "failed"
                elif not any(s == "building" for s in states):
                    # 已提交 chroot 全部完成但仍有 chroot 未提交（首次提交就是子集，§8.1）
                    # → 回到 build_main 由门控补交剩余 chroot
                    main_result["status"] = "interrupted"
                    write_json(main_result_path, main_result)
                    main_status = "interrupted"
                else:
                    write_json(main_result_path, main_result)
                    return ("wait", f"{PKGNAME}(build_id={build_id})", 60)
            elif build_id:
                # 旧路径（单 chroot / 无 per-chroot 记账）：单状态轮询，行为不变
                copr_state = _poll_copr_build(build_id, sd)
                if copr_state == "succeeded":
                    main_result["status"] = "success"
                    write_json(main_result_path, main_result)
                    _record_built_pkg(sd, wf, PKGNAME)
                    main_status = "success"
                elif copr_state == "failed":
                    main_result["status"] = "failed"
                    write_json(main_result_path, main_result)
                    main_status = "failed"
                else:
                    return ("wait", f"{PKGNAME}(build_id={build_id})", 60)
            else:
                main_result["status"] = "interrupted"
                write_json(main_result_path, main_result)
                main_status = "interrupted"

        if main_status in (None, "dep_needed", "precheck_done", "interrupted"):
            dispatch_main = True
            if tracking:
                # 提交门控（§8.1）：S = 依赖全部 ready_for(c) 且主包在 c 未终态的 chroot 子集
                ch_status = main_result.get("chroot_status") if isinstance(main_result, dict) else None
                done_cs = {c for c, i in (ch_status or {}).items()
                           if isinstance(i, dict) and i.get("status") in _CHROOT_CLOSED_STATUSES}
                # 依赖已 skipped 的 chroot 对主包不可达（级联放弃，避免无限等待）
                doomed = {c for c in targets if any(
                    isinstance(v.get("chroots"), dict) and isinstance(v["chroots"].get(c), dict)
                    and v["chroots"][c].get("status") == "skipped" for v in reg.values())}
                S = [c for c in targets if c not in done_cs and c not in doomed
                     and all(_ready_for(v, c) for v in reg.values())]
                if not S:
                    settled = done_cs | doomed
                    if targets and all(c in settled for c in targets):
                        # 全部 chroot 已终态（成功或跳过）：级联 skipped 记账后有成功 →
                        # success（落到下方 CI/feedback/done），全 skipped → fail
                        if isinstance(main_result, dict):
                            ch = main_result.setdefault("chroot_status", {})
                            for c in sorted(doomed - done_cs):
                                ch[c] = {"status": "skipped", "build_id": None}
                            if any(isinstance(ch.get(c), dict)
                                   and ch[c].get("status") in ("build_done", "reused") for c in targets):
                                main_result["status"] = "success"
                                write_json(main_result_path, main_result)
                                main_status = "success"
                                dispatch_main = False
                            else:
                                return ("fail", f"{PKGNAME} 全部目标 chroot 均被跳过", None)
                        else:
                            return ("fail", f"{PKGNAME} 全部目标 chroot 均被跳过", None)
                    else:
                        # 就绪 chroot 均已构建，其余 chroot 依赖未就绪 →
                        # 等待依赖按 chroot 推进（下轮晋升补交，§8.1）
                        return ("wait", f"{PKGNAME}(等待依赖按 chroot 就绪)", 60)
                else:
                    _DISPATCH_EXTRA["chroots"] = S
            if dispatch_main:
                lang = get_lang(sd, PKGNAME)
                return ("build_main", PKGNAME, build_delay(lang))

        if main_status in ("failed", "ci_failed"):
            build_id = main_result.get("copr_build_id") if main_result else None
            # 多 chroot：按失败 chroot 派发 analyze（§8.2），fc = 本轮处理的失败 chroot（None=旧路径）
            ch_status = main_result.get("chroot_status") if isinstance(main_result, dict) else None
            per_chroot = tracking and isinstance(ch_status, dict) and bool(ch_status)
            failed_chroots = [c for c in targets
                              if isinstance(ch_status.get(c), dict) and ch_status[c].get("status") == "failed"] \
                if per_chroot else []
            if per_chroot and not failed_chroots:
                # 全部 chroot 均已 skipped（超限/skip_chroot/级联），无可分析对象 → fail
                return ("fail", main_result.get("failure_reason", "全部目标 chroot 均被跳过"), None)
            fc = failed_chroots[0] if failed_chroots else None

            # builder 自检失败（从未提交 COPR，无 build_id）：fixer 无任何可诊断输入
            # （无 build_id / 无 build_failure / 无 submitted 快照），回 builder 重建
            # （SUBMODE=builder），重试 MAX_NO_OUTPUT_ROUNDS 次后 fail
            if main_status == "failed" and not build_id:
                if _fix_rounds(sd, PKGNAME) >= MAX_NO_OUTPUT_ROUNDS:
                    return ("fail",
                            f"{PKGNAME} builder 自检连续失败（未提交 COPR），已达重试上限", None)
                _bump_fix_round(sd, PKGNAME)
                lang = get_lang(sd, PKGNAME)
                return ("build_main", PKGNAME, build_delay(lang))

            if fc:
                # 失败 chroot 对应的 build_id（增量重交后各 chroot build_id 可能不同）
                build_id = ch_status[fc].get("build_id") or build_id

            # MISMATCH 二次：job_runner 已在 fix_state.mismatch_count 计数，重生成一次仍
            # mismatch 说明根因不在 spec 文本。多 chroot 按 (pkg, chroot) 计，
            # 超限仅放弃该 chroot（§8.2），旧路径维持直接 fail
            if main_status == "failed" and main_result and \
               "Package name mismatch" in main_result.get("failure_reason", ""):
                mc = int(_read_fix_state(sd, PKGNAME, fc).get("mismatch_count", 0) or 0)
                if mc >= 2:
                    if fc:
                        return _skip_main_chroot(sd, wf, reg, main_result, main_result_path,
                                                 PKGNAME, fc, targets,
                                                 f"第 {mc} 次 Package name mismatch，该 chroot 强制跳过")
                    return ("fail", f"{PKGNAME} 第 {mc} 次 Package name mismatch，强制 abort", None)

            analysis_file, legacy_shared = _resolve_analysis_file(sd, PKGNAME, build_id, fc)
            if not analysis_file.exists():
                return _emit_fix_action(sd, "fix_failure", PKGNAME, build_id, chroot=fc)
            analysis_data = read_json(analysis_file)
            verdict = analysis_data.get("verdict", "abort")
            if legacy_shared:
                # 多 chroot 共享 build_id 时旧命名 analysis 只消费一次，避免下一失败 chroot 误用
                analysis_file.unlink(missing_ok=True)
            if verdict == "skip_chroot" and fc:
                # analyzer 判定该 chroot 架构性不可构建 → 放弃该 chroot 而不是拖垮整个 job（§8.2）
                return _skip_main_chroot(sd, wf, reg, main_result, main_result_path,
                                         PKGNAME, fc, targets,
                                         analysis_data.get("reason", f"analyzer 判定 chroot {fc} 不可构建"))
            if verdict == "regenerate":
                # fixer 已删除 spec，回到 build_main 由 pkg-builder 重新生成。
                # 手术式更新（保留 copr_build_id/build_log 等历史字段），
                # 清零修复计数（重新生成是重置事件；mismatch_count 保留以防死循环）
                main_result["status"] = "interrupted"
                main_result.pop("chroot_status", None)  # 全量重生成：per-chroot 记账一并作废
                write_json(main_result_path, main_result)
                _clear_fix_counters(sd, PKGNAME, "fix_round", "no_output_rounds")
                # per-chroot 计数同样清零（mismatch_count 各维度均保留）
                _raw = _read_fix_state(sd, PKGNAME)
                if isinstance(_raw.get("chroots"), dict):
                    for _sub in _raw["chroots"].values():
                        if isinstance(_sub, dict):
                            _sub.pop("fix_round", None)
                            _sub.pop("no_output_rounds", None)
                    _write_fix_state(sd, PKGNAME, _raw)
                lang = get_lang(sd, PKGNAME)
                return ("build_main", PKGNAME, build_delay(lang))
            if verdict in ("rebuild", "retry-dep"):
                # retry-dep（注册了 required_by=主包的新依赖）视同 rebuild 走未重交逻辑：
                # 正常路径下 fixer 已重新提交（build_rpm_result 变为 copr_running），
                # 不会进入本分支（走上方 copr_running 轮询）。进入本分支 = fixer 未重提交，
                # dep 全就绪后再唤起 fixer（fix 模式）把可用依赖加入 BuildRequires。
                if _fix_rounds(sd, PKGNAME, fc) >= MAX_FIX_ROUNDS:
                    if fc:
                        return _skip_main_chroot(sd, wf, reg, main_result, main_result_path,
                                                 PKGNAME, fc, targets,
                                                 f"修复轮数达到上限 {MAX_FIX_ROUNDS}，该 chroot 强制跳过")
                    return ("fail", f"{PKGNAME} 修复轮数达到上限 {MAX_FIX_ROUNDS}，强制 abort", None)
                state = _read_fix_state(sd, PKGNAME, fc)
                n = int(state.get("no_output_rounds", 0) or 0) + 1
                if n >= MAX_NO_OUTPUT_ROUNDS:
                    if fc:
                        return _skip_main_chroot(sd, wf, reg, main_result, main_result_path,
                                                 PKGNAME, fc, targets,
                                                 f"连续 {n} 轮修复无产出，该 chroot 强制跳过")
                    return ("fail", f"{PKGNAME} 连续 {n} 轮修复无产出，强制 abort", None)
                _set_fix_counter(sd, PKGNAME, "no_output_rounds", n, chroot=fc)
                return _emit_fix_action(sd, "fix_failure", PKGNAME, build_id, chroot=fc)
            if verdict in ("retry", "retry-transient"):
                # fixer 已自行重提交时不会走到这里；走到这里 = 未重交，手术式重置为
                # interrupted（保留历史字段），由 build_main 重走（spec 存在 → resubmit）
                if _fix_rounds(sd, PKGNAME, fc) >= MAX_FIX_ROUNDS:
                    if fc:
                        return _skip_main_chroot(sd, wf, reg, main_result, main_result_path,
                                                 PKGNAME, fc, targets,
                                                 f"修复轮数达到上限 {MAX_FIX_ROUNDS}，该 chroot 强制跳过")
                    return ("fail", f"{PKGNAME} 修复轮数达到上限 {MAX_FIX_ROUNDS}，强制 abort", None)
                main_result["status"] = "interrupted"
                # 多 chroot：仅重试失败 chroot——chroot_status 中已成功 chroot 的
                # 记账保留，build_main 门控会把它们排除在重交子集之外
                write_json(main_result_path, main_result)
                lang = get_lang(sd, PKGNAME)
                return ("build_main", PKGNAME, build_delay(lang))
            reason = analysis_data.get("reason", f"main build {main_status}")
            if fc:
                reason = f"[{fc}] {reason}"
            return ("fail", reason, None)

        if main_status == "success":
            # CI 门禁：构建成功后必须通过（依赖闭合 + 可安装性 + 编译期依赖）
            ci_result_path = sd / f"pkgs/{PKGNAME}/ci_check_result.json"
            if not ci_result_path.exists():
                # 防死循环：CI 结果写入路径异常时会无限重跑 verify_install。
                # 已执行次数从 timeline 派生（读路径不递增计数，避免重复调用烧掉预算）
                attempts = _ci_runs_for_current_build(sd)
                if attempts >= MAX_CI_ATTEMPTS:
                    return ("fail",
                            f"ci_check_result.json 缺失，verify_install 已执行 {attempts} 次仍无结果",
                            None)
                return ("verify_install", PKGNAME, 0)
            ci_result = read_json(ci_result_path)
            ci_status = ci_result.get("status")
            if ci_status in ("error", "timeout"):
                # 环境/网络类失败（repoclosure/dnf 超时、工具缺失、脚本异常）：
                # 包本身未必有问题，重跑 CI 而非送 fixer 误修；受 MAX_CI_ATTEMPTS 熔断。
                # "timeout" 是 agent 侧 Bash 300s 超时杀掉 run_ci_check.py 后
                # 代写的结果（run_ci_check 自身只会写 pass/fail/error），同属环境性
                attempts = _ci_runs_for_current_build(sd)
                if attempts >= MAX_CI_ATTEMPTS:
                    errs = "; ".join(ci_result.get("errors", []))[:300]
                    return ("fail",
                            f"CI 检查连续 {attempts} 次环境性失败（超时/网络），"
                            f"构建本身已成功: {errs}",
                            None)
                return ("verify_install", PKGNAME, 0)
            if ci_status != "pass":
                main_result["status"] = "ci_failed"
                main_result["ci_errors"] = ci_result.get("errors", [])
                write_json(main_result_path, main_result)
                return _emit_fix_action(sd, "fix_failure", PKGNAME,
                                        main_result.get("copr_build_id"))
            # 跳过 critique，直接 feedback → summary → done
            feedback_file = sd / f"pkgs/{PKGNAME}/feedback_{PKGNAME}.json"
            if not feedback_file.exists():
                return ("feedback", PKGNAME, 60)
            summary_file = sd / f"pkgs/{PKGNAME}/{PKGNAME}_introduction_report.md"
            if not summary_file.exists():
                return ("summary", PKGNAME, 60)
            return ("done", PKGNAME, None)

        return ("fail", f"unexpected main_status: {main_status}", None)

    return ("fail", "unexpected dep_registry state", None)


def _satisfies_constraint(version: str, constraint: str) -> bool:
    """检查 version 是否满足 constraint 约束字符串（如 '>= 1.0, != 1.2'）。"""
    if not version or not constraint:
        return True
    try:
        from packaging.version import Version
        from packaging.specifiers import SpecifierSet
        return Version(version) in SpecifierSet(constraint)
    except Exception:
        return True  # 无法解析时保守认为满足


def _get_resolved_version(sd: Path, pkgname: str) -> str:
    """从 gate_result 读取已解析版本，找不到返回空串。"""
    gate_f = sd / f"pkgs/{pkgname}/gate_result_{pkgname}.json"
    if gate_f.exists():
        return read_json(gate_f).get("result", {}).get("version", "")
    return ""


def _downgrade_stale_deps(sd: Path, reg: dict) -> bool:
    """扫描 dep_registry，将 resolved_version 不满足当前 constraint 的 ready dep 降回 pending_evaluate。

    返回 True 表示有 dep 被降级（调用方需写回文件）。
    """
    changed = False
    for pkg, entry in reg.items():
        if entry.get("status") not in DEP_READY_STATUSES:
            continue
        constraint = entry.get("constraint", "")
        if not constraint:
            continue
        resolved = entry.get("resolved_version") or _get_resolved_version(sd, pkg)
        if resolved and not _satisfies_constraint(resolved, constraint):
            entry["status"] = "pending_evaluate"
            entry.pop("resolved_version", None)
            entry.pop("chroots", None)  # 版本降级后需全 chroot 重建，per-chroot 记账一并作废
            changed = True
    return changed


def update_after_evaluate_main(sd: Path, wf: dict, wf_path: Path, gate_decision: str) -> None:
    """主包 evaluate_main 完成后更新 workflow。"""
    PKGNAME = wf["pkgname"]
    if gate_decision in ("reuse_official", "reuse_copr_project",
                         "reuse_additional_repo"):
        wf.setdefault("reused_pkgs", [])
        if PKGNAME not in wf["reused_pkgs"]:
            wf["reused_pkgs"].append(PKGNAME)
        wf["goal_achieved"] = True  # 下一轮 determine_action 直接返回 done
    elif gate_decision in ("introduce_new", "introduce_new_with_ref",
                           "reuse_eur_srpm", "evaluate"):
        pass  # gate_result 文件已存在，需要走 COPR 构建
        # 注："evaluate" 是 gate 在约束无法解析时的兜底返回值，语义上视为需引入
    else:
        # gate 失败（空 decision 或未知值）：写入 evaluate_failed，等待 AI 分析
        wf["evaluate_failed"] = gate_decision or "evaluate_main gate failed"
    wf["loop_count"] = wf.get("loop_count", 0) + 1
    write_json(wf_path, wf)


def update_after_evaluate(sd: Path, reg: dict, reg_path: Path, target: str, gate_decision: str) -> None:
    """evaluate 完成后更新 dep_registry。"""
    if gate_decision in ("reuse_official", "reuse_copr_project",
                         "reuse_additional_repo"):
        reg[target]["status"] = "reused"
        # 记录实际解析版本，用于后续约束降级检查
        v = _get_resolved_version(sd, target)
        if v:
            reg[target]["resolved_version"] = v
    elif gate_decision in ("introduce_new", "introduce_new_with_ref",
                           "reuse_eur_srpm", "upgrade_user_repo", "evaluate"):
        reg[target]["status"] = "evaluate_done"
        # 注："evaluate" 是 gate 在约束无法解析时的兜底返回值，语义上视为需引入
    else:
        # gate 失败：写入 evaluate_failed，等待 AI 分析
        reg[target]["status"] = "evaluate_failed"
        reg[target]["error"] = gate_decision or "evaluate gate failed"
    write_json(reg_path, reg)


def update_after_build(
    sd: Path, wf: dict, wf_path: Path, reg: dict, reg_path: Path,
    target: str, build_status: str, is_dep: bool
) -> None:
    """build_dep / build_main 完成后更新状态。

    多 chroot（新 session）：按 build_rpm_result 的 copr_chroots/copr_build_ids
    把本轮提交的 chroot 子集记入 per-chroot 记账（dep → reg 条目 chroots 键；
    主包 → build_rpm_result.chroot_status），包级 status 由聚合重算（旧词表）。
    """
    tracking = _chroot_tracking(sd)
    targets = _target_chroots(sd) if tracking else []
    result_p = sd / f"pkgs/{target}/build_rpm_result.json"
    result = None
    if tracking and result_p.exists():
        try:
            result = read_json(result_p)
        except Exception:
            result = None

    def _submitted_chroots() -> list[str]:
        """本轮实际提交的 chroot 子集（copr_chroots 优先，旧单值 copr_chroot 兜底）。"""
        if not isinstance(result, dict):
            return []
        submitted = result.get("copr_chroots")
        if isinstance(submitted, list) and submitted:
            return [str(c) for c in submitted]
        single = result.get("copr_chroot", "")
        return [single] if single else []

    def _record_submitted(ch_map: dict, status: str) -> None:
        """把本轮提交的 chroot 记入 ch_map（逐 chroot build_id 优先，包级 copr_build_id 兜底）。"""
        bids = result.get("copr_build_ids") if isinstance(result, dict) else None
        bid = result.get("copr_build_id") if isinstance(result, dict) else None
        for c in _submitted_chroots():
            ch_map[c] = {"status": status,
                         "build_id": (bids.get(c) if isinstance(bids, dict) else None) or bid}

    if build_status == "success":
        if is_dep:
            if tracking:
                entry = reg[target]
                ch = entry.setdefault("chroots", {})
                _record_submitted(ch, "build_done")
                for c in targets:
                    ch.setdefault(c, {"status": "pending"})
                # 全部目标 chroot 就绪 → build_done；尚有未提交 chroot → evaluate_done 补交
                entry["status"] = _refresh_pkg_status(entry, targets)
            else:
                reg[target]["status"] = "build_done"
            write_json(reg_path, reg)
        elif tracking and isinstance(result, dict):
            # 主包：记录逐 chroot 成功；尚有 chroot 未提交 → interrupted 回 build_main 补交
            ch = result.setdefault("chroot_status", {})
            _record_submitted(ch, "build_done")
            states = [ch[c].get("status", "pending") if isinstance(ch.get(c), dict) else "pending"
                      for c in targets]
            if not all(s in _CHROOT_CLOSED_STATUSES for s in states):
                result["status"] = "interrupted"
            write_json(result_p, result)
        wf.setdefault("built_pkgs", [])
        if target not in wf["built_pkgs"]:
            wf["built_pkgs"].append(target)

    elif build_status == "copr_running":
        # COPR 构建已提交但 wait_for_build 超时，从 build_rpm_result.json 读 copr_build_id
        copr_build_id = None
        if result_p.exists():
            copr_build_id = read_json(result_p).get("copr_build_id")
        if is_dep:
            reg[target]["status"] = "copr_running"
            if copr_build_id:
                reg[target]["copr_build_id"] = copr_build_id
            if tracking:
                entry = reg[target]
                ch = entry.setdefault("chroots", {})
                _record_submitted(ch, "building")
                for c in targets:
                    ch.setdefault(c, {"status": "pending"})
            write_json(reg_path, reg)
        # 主包的 copr_running 直接保留在 build_rpm_result.json 里，supervisor 轮询时读取
        # （多 chroot 逐 chroot 记账由轮询分支从 copr_chroots/copr_build_ids 惰性建立）

    elif build_status == "dep_needed":
        # 新 dep 已写入 dep_registry，重新读取；把当前 target 标为 pending_deps
        # 等其前置依赖全部就绪后，determine_action 会自动升回 evaluate_done
        reg_new = read_json(reg_path)
        reg.clear()
        reg.update(reg_new)
        if is_dep and target in reg:
            reg[target]["status"] = DEP_WAITING_STATUS
        # 扫描并降级：reused/build_done 但 resolved_version 不满足最新 constraint
        _downgrade_stale_deps(sd, reg)
        write_json(reg_path, reg)

    elif build_status in ("precheck_done", "interrupted") or build_status not in VALID_BUILD_STATUSES:
        # 构建未完成，保持 evaluate_done，下次重建
        print(f"[warn] {target} build_rpm_result.status={build_status!r}, will retry", file=sys.stderr)

    else:
        # failed / ci_failed
        if is_dep:
            reg[target]["status"] = "build_failed"
            reg[target]["error"] = build_status
            if tracking:
                entry = reg[target]
                ch = entry.setdefault("chroots", {})
                _record_submitted(ch, "failed")
                for c in targets:
                    ch.setdefault(c, {"status": "pending"})
                # 同步路径只能拿到包级成败：按 chroot 轮询细化（部分 chroot 可能成功），
                # 轮询不到则保持全部 submitted=failed（保守，等价旧行为）
                _refine_failed_chroots(sd, ch)
                entry["status"] = _refresh_pkg_status(entry, targets)
                failed_cs = [c for c in targets
                             if isinstance(ch.get(c), dict) and ch[c].get("status") == "failed"]
                if failed_cs:
                    entry["error"] = f"{build_status}: chroot {','.join(failed_cs)}"
            write_json(reg_path, reg)
        elif tracking and isinstance(result, dict):
            # 主包：按 chroot 细化失败集合，供 Priority 4 逐失败 chroot 派发 analyze
            ch = result.setdefault("chroot_status", {})
            _record_submitted(ch, "failed")
            _refine_failed_chroots(sd, ch)
            write_json(result_p, result)


_STATUS_LABEL: dict[str, str] = {
    "pending_evaluate": "待评估",
    "evaluate_done":    "待构建",
    "pending_deps":     "等待依赖",
    "reused":           "复用(跳过)",
    "build_done":       "构建完成",
    "build_failed":     "构建失败",
    "copr_running":     "COPR构建中",
    "vendor_only":      "vendor(已满足)",
}

_MAIN_STATUS_LABEL: dict[str, str] = {
    None:           "待构建",
    "dep_needed":   "缺少依赖",
    "precheck_done":"预检完成",
    "interrupted":  "中断(待重建)",
    "success":      "构建成功",
    "failed":       "构建失败",
    "ci_failed":    "CI失败",
}


def print_progress(sd: Path, wf: dict, reg: dict, next_action: str, next_target: str) -> None:
    """向 CLI 打印本轮进展摘要。"""
    PKGNAME = wf["pkgname"]
    loop = wf.get("loop_count", 0) + 1
    # 主包状态
    main_result_path = sd / f"pkgs/{PKGNAME}/build_rpm_result.json"
    if main_result_path.exists():
        raw = read_json(main_result_path).get("status")
        main_status = raw if raw in VALID_BUILD_STATUSES else None
    else:
        main_status = None
    main_label = _MAIN_STATUS_LABEL.get(main_status, main_status or "待构建")

    # feedback 状态
    feedback_file = sd / f"pkgs/{PKGNAME}/feedback_{PKGNAME}.json"
    review_label = "feedback完成" if feedback_file.exists() else "-"

    sep = "─" * 56
    print(f"\n{sep}")
    print(f"  包引入进展  [{PKGNAME}]  第 {loop} 步")
    print(sep)
    print(f"  主包  {PKGNAME:<30} {main_label}  review: {review_label}")

    total_deps = len(reg)
    if total_deps:
        done_deps = sum(1 for v in reg.values() if v["status"] in DEP_READY_STATUSES)
        failed_deps_list = [k for k, v in reg.items() if v["status"] == "build_failed"]
        print(f"  依赖  共 {total_deps} 个，已就绪 {done_deps} 个"
              + (f"，失败 {len(failed_deps_list)} 个: {failed_deps_list}" if failed_deps_list else ""))
        # 逐条打印非就绪依赖（减少噪音，只展示未完成的）
        pending_deps_list = [(k, v) for k, v in reg.items() if v["status"] not in DEP_READY_STATUSES]
        if pending_deps_list:
            print("  ┌─ 未完成依赖:")
            for dep_name, dep_info in pending_deps_list:
                label = _STATUS_LABEL.get(dep_info["status"], dep_info["status"])
                required_by = dep_info.get("required_by", "")
                by_str = f"  ← {required_by}" if required_by and required_by != PKGNAME else ""
                print(f"  │  {dep_name:<30} {label}{by_str}")
            print("  └─")
    else:
        print("  依赖  无")

    print(f"  → 下一步: {next_action}({next_target})")
    print(sep + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="import-package-step 状态机")
    parser.add_argument("--session-dir", required=True)

    # 更新模式参数
    parser.add_argument("--update-action", choices=["evaluate_main", "evaluate", "build_dep", "build_main", "done", "fail"])
    parser.add_argument("--update-target", default="")
    parser.add_argument("--gate-decision", default="")   # evaluate 完成后
    parser.add_argument("--build-result", default="")    # build 完成后

    args = parser.parse_args()
    sd = Path(args.session_dir)

    wf_files = list(sd.glob("workflow_*.json"))
    if not wf_files:
        print(json.dumps({"error": "no workflow file found"}))
        return 1
    wf_path = wf_files[0]
    wf = read_json(wf_path)
    PKGNAME = wf["pkgname"]

    reg_path = sd / "dep_registry.json"
    reg = read_json(reg_path) if reg_path.exists() else {}

    # ── 更新模式 ──────────────────────────────────────────────────────────────
    if args.update_action:
        # 快照：记录更新前的所有状态
        snap_before = _snapshot_statuses(sd)

        if args.update_action == "evaluate_main":
            update_after_evaluate_main(sd, wf, wf_path, args.gate_decision)
            # 主包 evaluate 完成：记录主包状态转移
            diff_and_write_transitions(sd, snap_before)
            print(json.dumps({"updated": True}))
            return 0

        elif args.update_action == "evaluate":
            update_after_evaluate(sd, reg, reg_path, args.update_target, args.gate_decision)

        elif args.update_action in ("build_dep", "build_main"):
            is_dep = args.update_action == "build_dep"
            update_after_build(
                sd, wf, wf_path, reg, reg_path,
                args.update_target, args.build_result, is_dep
            )

        elif args.update_action == "done":
            wf["goal_achieved"] = True

        elif args.update_action == "fail":
            wf["goal_achieved"] = False
            wf["error"] = args.update_target
            if not wf["error"] or wf["error"] == PKGNAME:
                # 调用方未传有效 reason（如约定了传 reason 却传了包名）时兜底推导，
                # 避免前端只收到一个包名作为"失败原因"
                wf["error"] = _derive_fail_reason(sd, wf, reg, PKGNAME)

        wf["loop_count"] = wf.get("loop_count", 0) + 1
        write_json(wf_path, wf)

        # diff + 写 state.transition 事件
        diff_and_write_transitions(sd, snap_before)

        print(json.dumps({"updated": True}))
        return 0

    # ── 读状态模式：输出下一步 action ─────────────────────────────────────────
    # 快照：记录 determine_action 前的状态（COPR 轮询、vendor_only 判定等可能改变状态）
    snap_before = _snapshot_statuses(sd)

    # 检查 dep 的非标准 status，打印警告
    for dep_name, dep_info in reg.items():
        if dep_info["status"] != "evaluate_done":
            continue
        dep_result_path = sd / f"pkgs/{dep_name}/build_rpm_result.json"
        if dep_result_path.exists():
            dep_status = read_json(dep_result_path).get("status")
            if dep_status and dep_status not in VALID_BUILD_STATUSES:
                print(f"[warn] dep {dep_name} non-standard status={dep_status!r}, will rebuild", file=sys.stderr)

    _DISPATCH_EXTRA.clear()
    action, target, delay = determine_action(sd, wf, reg)
    loop = wf.get("loop_count", 0) + 1

    # build_* 的子模式路由（原 SKILL.md 的 [ -f spec ] 启发式收回脚本，确定性判定）：
    #   resubmit = spec 存在且已有过 COPR 提交 → pkg-fixer 重交模式（计一轮修复，
    #              写 fix_context.json，trigger=resubmit，受 MAX_FIX_ROUNDS 熔断）；
    #   builder  = 首次构建 / regenerate 后 / builder 自检失败重试 → pkg-builder。
    submode = ""
    if action in ("build_dep", "build_main"):
        spec_exists = (sd / "pkgs" / target / f"{target}.spec").exists()
        build_id = _current_build_id(sd, target, reg)
        if spec_exists and build_id:
            if _fix_rounds(sd, target) >= MAX_FIX_ROUNDS:
                action, target, delay = (
                    "fail",
                    f"{target} 修复轮数达到上限 {MAX_FIX_ROUNDS}（含重交轮），强制 abort",
                    None,
                )
            else:
                submode = "resubmit"
                _bump_fix_round(sd, target)
                ctx = fix_context(sd, target, build_id, trigger="resubmit")
                write_json(sd / "pkgs" / target / "fix_context.json", ctx)
        else:
            submode = "builder"

    # diff + 写 state.transition 事件
    diff_and_write_transitions(sd, snap_before)

    print_progress(sd, wf, reg, action, target)

    # evaluate action 时附带 constraint，供 evaluator 做版本选择
    constraint = ""
    if action == "evaluate" and target in reg:
        entry = reg[target]
        constraint = entry.get("constraint", "") if isinstance(entry, dict) else ""

    # 白名单校验：包名类 action 的 target 只允许合法包名字符，防止换行注入污染 agent prompt
    #（ROS 三 action 的 target 同样来自用户输入，纳入校验）
    _PKG_ACTIONS = {"evaluate_main", "evaluate", "resolve_upstream",
                    "build_main", "build_dep", "fix_failure", "fix_failure_dep",
                    "verify_install", "feedback", "analyze_evaluate_main", "analyze_evaluate",
                    "ros_prep", "ros_fetch", "ros_spec"}
    if action in _PKG_ACTIONS and not re.fullmatch(r'[a-zA-Z0-9._+\-]{1,128}', target):
        print(f"[security] target 格式非法，强制 fail: {target!r}", file=sys.stderr)
        action, target, delay = "fail", f"invalid target name: {target!r}", None

    result = {"action": action, "target": target, "delay": delay, "loop": loop, "pkgname": PKGNAME, "constraint": constraint, "submode": submode}
    # 多 chroot 派发参数（§8.1/§8.2）：build 步骤输出本轮可提交子集
    # COPR_BUILD_CHROOTS，fix/analyze 步骤输出失败 chroot CHROOT；
    # job_runner 读取后注入环境变量（构建脚本优先级 COPR_BUILD_CHROOTS > COPR_CHROOTS
    # > COPR_CHROOT），旧 job_runner 对未知键自然忽略
    if _DISPATCH_EXTRA.get("chroots"):
        result["copr_build_chroots"] = ",".join(_DISPATCH_EXTRA["chroots"])
    if _DISPATCH_EXTRA.get("chroot"):
        result["chroot"] = _DISPATCH_EXTRA["chroot"]
    for k, v in result.items():
        print(f"{k.upper()}={shlex.quote(str(v) if v is not None else '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
