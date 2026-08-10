#!/usr/bin/env python3
"""pkg-fixer 修复后重新提交构建（原子完成，中途失败报错退出，不留半成品状态）。

默认路径（修改过 spec 的 rebuild/resubmit）：
  1. spec 新于 build/SOURCES/ 下对应 tarball 时，按 fixer 文档规则重打 tarball
     （tar --hard-dereference + --transform）
  2. rpmbuild -bs --nodeps（_topdir=session/build, _srcrpmdir=session/srpms），
     解析输出 "Wrote: " 得到本次 SRPM 精确路径（禁止 glob 挑选）
  3. 防陈旧闸门：校验 SRPM 内嵌 spec 与当前 spec 一致
  4. copr_client.py 提交（--output pkgs/<pkg>/build_rpm_result.json）
  5. 提交成功（出现新 copr_build_id）后 cp spec 到 submitted_specs/spec_<新id>.spec

--reuse-srpm（仅 retry-transient 原样重交）：
  取 srpms/ 下最新 <pkg>-*.src.rpm 直接提交，跳过重打和闸门。

退出码：
  0  成功
  1  参数/环境错误（session.json、spec、copr_client 缺失，chroot 未知等）
  2  tarball 重建失败
  3  rpmbuild -bs 失败或输出中无 "Wrote: " 路径
  4  COPR 提交失败或未出现新 copr_build_id
  5  需要重打 tarball 但 sources/<pkg>/ 不存在
  6  防陈旧闸门未通过（SRPM 内嵌 spec 与当前 spec 不一致）
  7  --reuse-srpm 但 srpms/ 下无可用 SRPM
  8  ROS 依赖名门禁未通过（spec 含 ros-projects.list 外的幻觉依赖名）

用法：
  python3 submit_fix.py --session-dir . --pkg git [--chroots a-x86_64,a-aarch64] [--reuse-srpm]
  python3 submit_fix.py --session-dir . --pkg git [--chroot openeuler-24.03-x86_64] [--all-chroots]
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from timeline import write_event

# build-rpm skill 的 COPR 提交脚本（相对于本脚本：skills/build-rpm/scripts/copr_client.py）
_COPR_CLIENT = Path(__file__).resolve().parent.parent.parent / "build-rpm" / "scripts" / "copr_client.py"
# 复用 copr_client 的 chroot 解析/主 chroot 规则（lazy import，存在性由 main() 检查）
sys.path.insert(0, str(_COPR_CLIENT.parent))


def _fail(code: int, stage: str, msg: str) -> int:
    print(f"[submit_fix] FAIL[{stage}] {msg}", file=sys.stderr)
    return code


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _gate_version(sd: Path, pkg: str) -> str:
    """从 gate_result 读已解析版本（决定 tarball 命名），找不到返回空串。"""
    gate_f = sd / f"pkgs/{pkg}/gate_result_{pkg}.json"
    if gate_f.exists():
        try:
            return _read_json(gate_f).get("result", {}).get("version", "")
        except Exception:
            pass
    return ""


def _strip_tar_suffix(name: str) -> str:
    for suf in (".tar.gz", ".tar.xz", ".tar.bz2", ".tgz", ".tar"):
        if name.endswith(suf):
            return name[: -len(suf)]
    return name


def _ensure_tarball(sd: Path, pkg: str, spec_path: Path) -> int:
    """spec 新于对应 tarball（或 tarball 缺失）时按 fixer 文档规则重打。"""
    sources_dir = sd / "sources" / pkg
    build_sources = sd / "build" / "SOURCES"
    build_sources.mkdir(parents=True, exist_ok=True)
    spec_mtime = spec_path.stat().st_mtime

    version = _gate_version(sd, pkg)
    target = None
    prefix = ""
    if version:
        target = build_sources / f"{pkg}-{version}.tar.gz"
        prefix = f"{pkg}-{version}"
    else:
        # 无 version 时取 SOURCES 下最新候选 tarball 的名字作为目标
        candidates = sorted(build_sources.glob(f"{pkg}-*.tar.*"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            target = candidates[0]
            prefix = _strip_tar_suffix(target.name)

    need_rebuild = target is None or not target.exists() or target.stat().st_mtime < spec_mtime
    if not need_rebuild:
        return 0

    if not sources_dir.is_dir():
        return _fail(5, "tarball", f"需要重打 tarball 但 {sources_dir} 不存在")
    if not prefix:
        return _fail(1, "tarball", "无法确定 tarball 名称（gate_result 无 version 且 SOURCES 无候选）")

    print(f"[submit_fix] spec 新于 tarball，重打 {target.name}", file=sys.stderr)
    proc = subprocess.run(
        ["tar", "--hard-dereference", "-czf", str(target),
         "--transform", f"s|^./sources/{pkg}|{prefix}|",
         f"./sources/{pkg}/"],
        cwd=sd, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return _fail(2, "tarball", f"tar 重打失败: {proc.stderr.strip()[:500]}")
    return 0


def _build_srpm(sd: Path, pkg: str, spec_path: Path) -> tuple[Path | None, int]:
    """rpmbuild -bs，解析 'Wrote: ' 得本次 SRPM 精确路径（禁止 glob）。"""
    build_dir = sd / "build"
    srpms_dir = sd / "srpms"
    for d in (srpms_dir, build_dir / "SOURCES", build_dir / "SPECS"):
        d.mkdir(parents=True, exist_ok=True)
    spec_copy = build_dir / "SPECS" / f"{pkg}.spec"
    shutil.copy2(spec_path, spec_copy)

    proc = subprocess.run(
        ["rpmbuild", "-bs", "--nodeps",
         "--define", f"_topdir {build_dir.resolve()}",
         "--define", f"_srcrpmdir {srpms_dir.resolve()}",
         str(spec_copy)],
        cwd=sd, capture_output=True, text=True,
    )
    # 构建输出存档（对齐原 fixer 手敲流程的 tee -a build.log）
    log_path = sd / "pkgs" / pkg / "build.log"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(proc.stdout + proc.stderr)

    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-20:])
        return None, _fail(3, "rpmbuild", f"rpmbuild -bs 失败:\n{tail}")
    wrote = [l.split("Wrote:", 1)[1].strip() for l in proc.stdout.splitlines() if "Wrote:" in l]
    if not wrote:
        return None, _fail(3, "rpmbuild", "rpmbuild 输出中未找到 'Wrote: ' SRPM 路径")
    srpm = Path(wrote[-1])
    if not srpm.exists():
        return None, _fail(3, "rpmbuild", f"rpmbuild 报告的 SRPM 不存在: {srpm}")
    return srpm, 0


def _srpm_embedded_spec(srpm: Path, pkg: str) -> str | None:
    """提取 SRPM 内嵌 spec；rpm2cpio/cpio 不可用时返回 None。"""
    if not (shutil.which("rpm2cpio") and shutil.which("cpio")):
        return None
    p1 = subprocess.Popen(["rpm2cpio", str(srpm)], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL)
    p2 = subprocess.run(["cpio", "-i", "--quiet", "--to-stdout", "*.spec"],
                        stdin=p1.stdout, capture_output=True, text=True)
    p1.wait()
    if p1.returncode != 0 or p2.returncode != 0 or not p2.stdout.strip():
        return ""
    return p2.stdout


def _stale_gate(srpm: Path, pkg: str, spec_path: Path) -> int:
    """防陈旧闸门：SRPM 内嵌 spec 必须与当前 spec 一致。"""
    embedded = _srpm_embedded_spec(srpm, pkg)
    if embedded is None:
        # 容器无 rpm2cpio/cpio：退化为 mtime 比较 + rpmbuild 刚执行成功的事实
        if srpm.stat().st_mtime < spec_path.stat().st_mtime:
            return _fail(6, "stale-gate", "SRPM mtime 早于 spec mtime，且无法提取内嵌 spec 校验")
        print("[submit_fix] WARN: 无 rpm2cpio/cpio，防陈旧闸门退化为 mtime 校验", file=sys.stderr)
        return 0
    if embedded == "" or embedded != spec_path.read_text(encoding="utf-8"):
        return _fail(6, "stale-gate", "SRPM 内嵌 spec 与当前 spec 不一致，拒绝提交陈旧构建")
    return 0


def _ros_deps_gate(sd: Path, pkg: str, spec_path: Path) -> int:
    """ROS 依赖名门禁：lang=ros 时校验 spec 中 ros-<distro>-* 依赖名真实存在。"""
    gate_f = sd / f"pkgs/{pkg}/gate_result_{pkg}.json"
    lang = ""
    if gate_f.exists():
        try:
            lang = _read_json(gate_f).get("lang", "") or ""
        except Exception:
            pass
    if lang != "ros":
        return 0
    verify = _COPR_CLIENT.parent / "verify_ros_spec_deps.py"
    if not verify.exists():
        print(f"[submit_fix] WARN: {verify} 缺失，跳过 ROS 依赖名门禁", file=sys.stderr)
        return 0
    proc = subprocess.run(
        [sys.executable, str(verify), str(spec_path), "--session-dir", str(sd)],
        cwd=sd, capture_output=True, text=True,
    )
    if proc.returncode == 1:
        return _fail(8, "ros-deps-gate",
                     f"ROS 依赖名门禁未通过（修正 spec 后重提）:\n{proc.stderr.strip()}")
    if proc.returncode != 0:
        print(f"[submit_fix] WARN: ROS 依赖名门禁执行异常（rc={proc.returncode}），降级放行: "
              f"{proc.stderr.strip()[:200]}", file=sys.stderr)
    return 0


def _failed_chroots(sd: Path, pkg: str, chroots: list) -> list:
    """dep_registry.json 中该包 per-chroot status == "failed" 的 chroot（保持入参顺序）。

    无 dep_registry / 无该包条目 / 条目无 per-chroot "chroots" 映射（旧单 chroot
    session）时返回 []，调用方退化为全量重交，行为与旧版一致。
    """
    reg_path = sd / "dep_registry.json"
    if not reg_path.exists():
        return []
    try:
        entry = _read_json(reg_path).get(pkg, {})
    except Exception:
        return []
    chroot_map = entry.get("chroots") if isinstance(entry, dict) else None
    if not isinstance(chroot_map, dict):
        return []
    return [c for c in chroots
            if isinstance(chroot_map.get(c), dict) and chroot_map[c].get("status") == "failed"]


def _resolve_chroots(args, session: dict, sd: Path) -> list:
    """解析本次重交的 chroot 集合。

    来源优先级：--chroots/--chroot 参数 > COPR_BUILD_CHROOTS 环境变量
    > session.json["copr_chroots"] > session.json["copr_chroot"]。
    未显式指定且未加 --all-chroots 时，默认只重交 dep_registry 中 failed 的
    chroot 子集（增量重建）；无 per-chroot 信息时退化为全量（旧行为）。
    """
    from copr_client import parse_chroots

    explicit = parse_chroots(args.chroots) or parse_chroots(args.chroot)
    if explicit:
        return explicit
    full = (parse_chroots(os.environ.get("COPR_BUILD_CHROOTS"))
            or parse_chroots(session.get("copr_chroots"))
            or parse_chroots(session.get("copr_chroot")))
    if not full or args.all_chroots:
        return full
    return _failed_chroots(sd, args.pkg, full) or full


def _submit(sd: Path, pkg: str, srpm: Path, chroots: list, session: dict) -> tuple[str, int]:
    """调 copr_client.py 提交；成功返回 (新 copr_build_id, 0)。"""
    from copr_client import primary_chroot

    result_json = sd / "pkgs" / pkg / "build_rpm_result.json"
    old_build_id = ""
    if result_json.exists():
        try:
            old_build_id = str(_read_json(result_json).get("copr_build_id", "") or "")
        except Exception:
            pass

    # copr_client 从环境变量读凭据（对齐 read-session.py 的字段映射）
    env = os.environ.copy()
    env.update({
        "COPR_FRONTEND_URL": session.get("copr_url", ""),
        "COPR_OWNER": session.get("copr_owner", ""),
        "COPR_PROJECT": session.get("copr_project", ""),
        "COPR_API_LOGIN": session.get("copr_login", ""),
        "COPR_API_TOKEN": session.get("copr_token", ""),
        "COPR_CHROOT": primary_chroot(chroots),
        "COPR_CHROOTS": ",".join(chroots),
    })
    proc = subprocess.run(
        [sys.executable, str(_COPR_CLIENT), str(srpm),
         "--output", str(result_json), "--chroots", ",".join(chroots)],
        cwd=sd, env=env, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-20:])
        return "", _fail(4, "submit", f"copr_client 提交失败:\n{tail}")

    try:
        new_build_id = str(_read_json(result_json).get("copr_build_id", "") or "")
    except Exception as e:
        return "", _fail(4, "submit", f"提交后无法读取 {result_json}: {e}")
    if not new_build_id or new_build_id == old_build_id:
        return "", _fail(4, "submit", f"提交后未出现新 copr_build_id（仍为 {old_build_id!r}）")
    return new_build_id, 0


def main() -> int:
    parser = argparse.ArgumentParser(description="fixer 修复后重新提交 COPR 构建（原子）")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--pkg", required=True)
    parser.add_argument("--chroot", default="",
                        help="单个目标 chroot（等价 --chroots 单元素；缺省从环境/session 解析）")
    parser.add_argument("--chroots", default="",
                        help="目标 chroot 列表，逗号分隔（显式指定时按原样提交，优先级最高）")
    parser.add_argument("--all-chroots", action="store_true",
                        help="全量重交所有目标 chroot（默认只重交 dep_registry 中 failed 的 chroot）")
    parser.add_argument("--reuse-srpm", action="store_true",
                        help="复用 srpms/ 下最新 SRPM 直接提交（仅 retry-transient）")
    args = parser.parse_args()

    sd = Path(args.session_dir)
    pkg = args.pkg
    pkg_dir = sd / "pkgs" / pkg
    spec_path = pkg_dir / f"{pkg}.spec"

    session_file = sd / "session.json"
    if not session_file.exists():
        return _fail(1, "env", f"session.json 不存在: {session_file}")
    session = _read_json(session_file)
    if not _COPR_CLIENT.exists():
        return _fail(1, "env", f"copr_client.py 不存在: {_COPR_CLIENT}")
    chroots = _resolve_chroots(args, session, sd)
    if not chroots:
        return _fail(1, "env", "chroot 未知（--chroots/--chroot 未给且环境/session.json 无 chroot 信息）")

    if args.reuse_srpm:
        # 原样重交：取 srpms/ 下最新 SRPM，跳过重打和闸门
        candidates = sorted((sd / "srpms").glob(f"{pkg}-*.src.rpm"),
                            key=lambda p: p.stat().st_mtime, reverse=True) \
            if (sd / "srpms").is_dir() else []
        if not candidates:
            return _fail(7, "reuse-srpm", f"srpms/ 下无 {pkg}-*.src.rpm 可复用")
        srpm = candidates[0]
        print(f"[submit_fix] 复用 SRPM: {srpm}", file=sys.stderr)
    else:
        if not spec_path.exists():
            return _fail(1, "env", f"spec 不存在: {spec_path}")
        rc = _ros_deps_gate(sd, pkg, spec_path)
        if rc != 0:
            return rc
        rc = _ensure_tarball(sd, pkg, spec_path)
        if rc != 0:
            return rc
        srpm, rc = _build_srpm(sd, pkg, spec_path)
        if rc != 0:
            return rc
        rc = _stale_gate(srpm, pkg, spec_path)
        if rc != 0:
            return rc

    new_build_id, rc = _submit(sd, pkg, srpm, chroots, session)
    if rc != 0:
        return rc

    # 显式重交标记：supervisor 以此判定"fixer 已重交"（替代 build_id 变化启发式）。
    # 必须在 copr_client --output 覆写之后写入；supervisor 消费后会清除该标记。
    result_json = sd / "pkgs" / pkg / "build_rpm_result.json"
    try:
        br = _read_json(result_json)
        br["resubmitted"] = True
        result_json.write_text(json.dumps(br, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[submit_fix] WARN: 无法写入 resubmitted 标记: {e}", file=sys.stderr)

    # ── 时间线：构建已提交 ────────────────────────────────────────────────
    spec_md5 = ""
    if spec_path.exists():
        spec_md5 = hashlib.md5(spec_path.read_bytes()).hexdigest()
    write_event(sd, "build.submitted", pkg, {
        "build_id": new_build_id,
        "chroots": chroots,
        "srpm": srpm.name,
        "submitted_by": "submit_fix.py",
        "spec_md5": spec_md5,
    })

    # spec 快照存档（地面真值，供下轮修复对照）；reuse 路径 spec 未变，同样记录
    if spec_path.exists():
        snapshot_dir = pkg_dir / "submitted_specs"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(spec_path, snapshot_dir / f"spec_{new_build_id}.spec")

    print(f"[submit_fix] OK: submitted build_id={new_build_id} chroots={','.join(chroots)} srpm={srpm.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
