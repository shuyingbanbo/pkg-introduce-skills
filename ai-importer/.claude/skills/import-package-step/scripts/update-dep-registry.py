#!/usr/bin/env python3
"""从 build_rpm_result.json 的 dep_needed 结果写入 dep_registry.json。

用法：
  python3 update-dep-registry.py --session-dir . --pkg hello-openeuler

若某个依赖已登记过且新旧约束不同：两者不冲突时自动合并为同时满足两者的
约束；冲突时（如已登记 ">=2.0"，新约束要求 "<1.5"）保留旧约束不覆盖，
并在 stdout 打印 conflicts 列表、以非零状态退出——与 register-dep.py 对
同一种情况的处理方式保持一致，不静默覆盖旧约束。一次调用可能同时处理
多个依赖，冲突只影响冲突的那一条，其余依赖仍正常写入。
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from constraint_conflict import has_conflict, merge_constraints  # noqa: E402

# 引入构建工具链约束
BUILD_RPM_SCRIPTS = Path(__file__).resolve().parents[2] / "build-rpm" / "scripts"
sys.path.insert(0, str(BUILD_RPM_SCRIPTS))
from chroot_toolchain import is_toolchain  # noqa: E402
from rpm_naming import rpm_name_from_gav  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--pkg", required=True)
    args = parser.parse_args()

    sd = Path(args.session_dir)
    result_path = sd / "pkgs" / args.pkg / "build_rpm_result.json"
    reg_path = sd / "dep_registry.json"

    if not result_path.exists():
        print(f"[update-dep-registry] build_rpm_result.json not found for {args.pkg}")
        return

    result = json.loads(result_path.read_text(encoding="utf-8"))
    reg = json.loads(reg_path.read_text(encoding="utf-8")) if reg_path.exists() else {}

    added = []
    updated = []

    # 从 pre_check reports 里建立包名→完整信息的索引（含 url/constraint）
    pre_check_index: dict = {}
    reports_dir = sd / "reports"
    pre_check_path = reports_dir / f"pre_check_{args.pkg}.json"
    if pre_check_path.exists():
        try:
            pc = json.loads(pre_check_path.read_text(encoding="utf-8"))
            for dep in pc.get("dependency_decisions", []):
                name = dep.get("name", "")
                if name:
                    # key 归一化（GAV → artifactId），与注册 key 保持一致
                    pre_check_index[rpm_name_from_gav(name)] = {
                        "url": dep.get("upstream_url", ""),
                        "constraint": dep.get("constraint", ""),
                    }
        except Exception:
            pass

    # 兼容两种格式：
    # 1. result["deps"] — 带 url/constraint 的完整列表（旧格式/未来扩展）
    # 2. result["dependency_resolution"]["pending_deps"] — 纯名称列表（precheck 输出）
    deps_full = result.get("deps", [])
    pending_names = result.get("dependency_resolution", {}).get("pending_deps", [])

    # 合并：full list 优先，pending_names 补充（从 pre_check_index 补全 url/constraint）
    # seen 与查找均用归一化后的名字（GAV → artifactId），与 pre_check_index 的 key 对齐
    seen = {rpm_name_from_gav(d["name"]) for d in deps_full if isinstance(d, dict)}
    deps = list(deps_full)
    for name in pending_names:
        if isinstance(name, str) and rpm_name_from_gav(name) not in seen:
            pc_info = pre_check_index.get(rpm_name_from_gav(name), {})
            deps.append({
                "name": name,
                "url": pc_info.get("url", ""),
                "constraint": pc_info.get("constraint", ""),
            })

    conflicts = []

    for dep in deps:
        # GAV 名归一化（com.google.guava:guava → guava），防止双名重复注册
        name = rpm_name_from_gav(dep["name"])
        # 构建工具链不得注册为依赖
        if is_toolchain(name):
            print(f"[update-dep-registry] skip toolchain: {name}")
            continue
        # 白名单校验：dep 名称只允许合法包名字符，防止换行注入污染 agent prompt
        if not re.fullmatch(r'[a-zA-Z0-9._+\-]{1,128}', name):
            print(f"[update-dep-registry] skip invalid dep name: {name!r}")
            continue
        new_constraint = dep.get("constraint", "")
        if name not in reg:
            # 新条目不带 chroots 键：evaluate 阶段 chroot 无关（§8.1），
            # per-chroot 状态由构建阶段的 step_supervisor 按需建立；
            # 已有条目走整字典读-改-写，chroots 等未知键天然保留。
            reg[name] = {
                "url": dep.get("url", ""),
                "constraint": new_constraint,
                "status": "pending_evaluate",
                "required_by": args.pkg,
            }
            added.append(name)
        else:
            old_constraint = reg[name].get("constraint", "")
            if new_constraint and new_constraint != old_constraint:
                if old_constraint:
                    conflict, reason = has_conflict(old_constraint, new_constraint)
                    if conflict:
                        conflicts.append({
                            "name": name,
                            "old_constraint": old_constraint,
                            "new_constraint": new_constraint,
                            "reason": reason,
                        })
                        continue
                    merged = merge_constraints(old_constraint, new_constraint)
                    if merged != old_constraint:
                        reg[name]["constraint"] = merged
                        updated.append(name)
                else:
                    reg[name]["constraint"] = new_constraint
                    updated.append(name)

    reg_path.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[update-dep-registry] added={added} updated={updated}")
    if conflicts:
        print(f"[update-dep-registry] conflicts={json.dumps(conflicts, ensure_ascii=False)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
