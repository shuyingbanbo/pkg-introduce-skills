#!/usr/bin/env python3
"""检测并合并版本约束字符串，供 register-dep.py 和 update-dep-registry.py 共用。

背景：两个脚本此前对"同一个依赖已登记约束、又收到一条不同约束"这种情况的处理
方式互相矛盾（register-dep.py 只在旧约束为空时才写入，update-dep-registry.py
则直接用新约束覆盖旧约束），且都不检测两个约束是否根本无法同时满足。
本模块提供统一的冲突检测 + 合并逻辑，两个脚本共用同一套判断。

只支持形如 '>=1.2.3'、'<2.0'、'==1.5'、'!=1.2' 的简单比较子句（可用逗号
拼接多个子句），这也是 dep_registry.json 里 constraint 字段的实际写法
（参考 step_supervisor.py 文档字符串里的例子 '>= 1.0, != 1.2'）。解析不出
结构化子句的约束（比如 npm 的 ^/~ 写法）保守地跳过冲突检测，不误报。
"""

from __future__ import annotations

import re
from typing import NamedTuple

_CLAUSE_RE = re.compile(r'(>=|<=|==|!=|>|<)\s*([0-9][0-9A-Za-z.+_~\-]*)')


class Clause(NamedTuple):
    op: str
    version: str


def _parse_clauses(constraint: str) -> list[Clause]:
    """把 '>=1.2, !=1.5' 这样的约束字符串拆成若干子句，解析不出来的部分忽略。"""
    if not constraint:
        return []
    return [Clause(op, ver) for op, ver in _CLAUSE_RE.findall(constraint)]


def _version_tuple(version: str) -> tuple:
    """轻量版本号转 tuple 用于比较，不依赖 packaging 库（这两个脚本只用标准库）。"""
    tokens: list = []
    for part in re.split(r"[^A-Za-z0-9]+", version):
        if not part:
            continue
        for tok in re.findall(r"[A-Za-z]+|\d+", part):
            tokens.append(int(tok) if tok.isdigit() else tok)
    return tuple(tokens)


# 预发布标记：同为该版本时预发布 < 正式版（PEP 440 语义）
_PRE_TOKENS = ("a", "alpha", "b", "beta", "rc", "pre", "preview", "dev")


def _cmp(a: str, b: str) -> int:
    ta, tb = list(_version_tuple(a)), list(_version_tuple(b))
    # 尾部补零对齐：2.0 与 2.0.0 应判相等
    n = max(len(ta), len(tb))
    ta += [0] * (n - len(ta))
    tb += [0] * (n - len(tb))
    for x, y in zip(ta, tb):
        if type(x) is not type(y):
            # 一侧为预发布标记、另一侧为数值（含补零）→ 预发布侧更小
            if isinstance(x, str) and isinstance(y, int):
                return -1 if x.lower() in _PRE_TOKENS else 1
            if isinstance(x, int) and isinstance(y, str):
                return 1 if y.lower() in _PRE_TOKENS else -1
            x, y = str(x), str(y)
        if x != y:
            return -1 if x < y else 1
    return 0


def has_conflict(old_constraint: str, new_constraint: str) -> tuple[bool, str]:
    """判断 old_constraint 和 new_constraint 是否存在版本区间不可能同时满足的冲突。

    返回 (is_conflict, reason)。任一约束解析失败时保守返回 (False, "")——
    宁可放过潜在冲突，也不能因误判阻塞正常的依赖登记流程。
    """
    if not old_constraint or not new_constraint or old_constraint == new_constraint:
        return False, ""

    clauses = _parse_clauses(old_constraint) + _parse_clauses(new_constraint)
    if not clauses:
        return False, ""

    lower: tuple[str, bool] | None = None   # (version, inclusive)
    upper: tuple[str, bool] | None = None
    exact_values: set[str] = set()
    not_equal: set[str] = set()

    try:
        for op, ver in clauses:
            if op in (">=", ">"):
                if lower is None or _cmp(ver, lower[0]) > 0:
                    lower = (ver, op == ">=")
            elif op in ("<=", "<"):
                if upper is None or _cmp(ver, upper[0]) < 0:
                    upper = (ver, op == "<=")
            elif op == "==":
                exact_values.add(ver)
            elif op == "!=":
                not_equal.add(ver)

        if len(exact_values) > 1:
            return True, f"约束要求同时等于 {sorted(exact_values)}，无法同时满足"

        if exact_values:
            (ev,) = tuple(exact_values)
            if ev in not_equal:
                return True, f"约束同时要求 =={ev} 和 !={ev}，无法同时满足"
            if lower is not None:
                c = _cmp(ev, lower[0])
                if c < 0 or (c == 0 and not lower[1]):
                    return True, f"约束要求 =={ev}，但下界为 {'>=' if lower[1] else '>'}{lower[0]}"
            if upper is not None:
                c = _cmp(ev, upper[0])
                if c > 0 or (c == 0 and not upper[1]):
                    return True, f"约束要求 =={ev}，但上界为 {'<=' if upper[1] else '<'}{upper[0]}"
            return False, ""

        if lower is not None and upper is not None:
            c = _cmp(lower[0], upper[0])
            if c > 0:
                return True, (
                    f"约束区间为空：{'>=' if lower[1] else '>'}{lower[0]} 与 "
                    f"{'<=' if upper[1] else '<'}{upper[0]} 无重叠"
                )
            if c == 0 and not (lower[1] and upper[1]):
                return True, (
                    f"约束区间为空（边界不含端点）：{'>=' if lower[1] else '>'}{lower[0]} 与 "
                    f"{'<=' if upper[1] else '<'}{upper[0]}"
                )
    except Exception:
        return False, ""

    return False, ""


def merge_constraints(old_constraint: str, new_constraint: str) -> str:
    """把两个已确认不冲突的约束合并为同时满足两者的组合约束字符串。

    调用方必须先用 has_conflict() 确认两者不冲突。合并结果与调用顺序无关
    （按子句去重后拼接），避免 registry 因为两个脚本的调用先后不同而漂移。
    """
    if not old_constraint:
        return new_constraint
    if not new_constraint:
        return old_constraint
    if old_constraint == new_constraint:
        return old_constraint

    seen_keys: set[str] = set()
    merged_parts: list[str] = []
    for part in (old_constraint, new_constraint):
        for op, ver in _parse_clauses(part):
            key = f"{op}{ver}"
            if key not in seen_keys:
                seen_keys.add(key)
                merged_parts.append(key)

    if not merged_parts:
        # 两边都不是可解析的结构化子句（如 npm 的 ^/~ 写法），保守地采用较新的一条
        return new_constraint
    return ", ".join(merged_parts)