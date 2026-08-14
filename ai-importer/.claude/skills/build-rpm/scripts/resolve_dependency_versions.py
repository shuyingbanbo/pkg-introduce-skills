#!/usr/bin/env python3
"""Resolution layer: build ResolutionPlan objects from DependencyRequest input.

Note: this module still contains `apply_finalize_result()` as a transition helper for the
execution layer, but long-term state mutation should move fully behind execution/state APIs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dependency_resolution_state import ensure_state_files, load_state, state_file_path
from providers.pypi import list_stable_versions as list_pypi_stable_versions, normalize_version
from providers.npm import list_stable_versions as list_npm_stable_versions
from constraint_parser import to_specifier_set, normalize_npm_constraint

try:
    from packaging.version import InvalidVersion, Version
except Exception:
    Version = None
    InvalidVersion = Exception

MAX_CANDIDATES = 3


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_planning_snapshot(build_state_dir: str, payload: dict[str, Any]) -> Path:
    path = state_file_path(build_state_dir, "session_snapshot").with_name("dependency_planning_history.json")
    history = []
    if path.exists():
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            history = []
    if not isinstance(history, list):
        history = []
    history.append(payload)
    write_json(path, {"history": history})
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def satisfies_constraint(version: str, constraint: str, constraint_type: str, requirement_info: dict[str, Any]) -> bool:
    normalized_version = normalize_version(version)
    if not normalized_version:
        return False
    if constraint_type == "unbounded":
        return True
    if constraint_type == "unknown":
        return False

    exact_version = normalize_version((requirement_info or {}).get("exact_version") or "")
    if constraint_type == "exact":
        return bool(exact_version) and normalized_version == exact_version

    specifier = to_specifier_set(constraint)
    if specifier is None or Version is None:
        return False
    try:
        return Version(normalized_version) in specifier
    except (InvalidVersion, Exception):
        return False


def attempted_versions_for(dep_name: str, attempts_state: dict[str, Any]) -> set[str]:
    attempted: set[str] = set()
    for item in (attempts_state.get(dep_name, {}) or {}).get("attempted_versions", []):
        if isinstance(item, dict) and item.get("version"):
            attempted.add(str(item["version"]))
    return attempted


def build_constraint_record(dep_item: dict[str, Any], requested_by: str) -> dict[str, Any]:
    return {
        "from": requested_by,
        "lang": dep_item.get("type") or "",
        "constraint": dep_item.get("constraint") or dep_item.get("requirement") or "",
    }


def resolve_candidates(dep_item: dict[str, Any], build_state_dir: str, requested_by: str) -> dict[str, Any]:
    """Build a ResolutionPlan for a single DependencyRequest."""
    ensure_state_files(build_state_dir)
    resolved_versions = load_state(build_state_dir, "resolved_versions")
    dependency_attempts = load_state(build_state_dir, "dependency_attempts")

    name = dep_item.get("name") or dep_item.get("dep") or ""
    constraint = dep_item.get("constraint") or dep_item.get("requirement") or ""
    constraint_type = dep_item.get("constraint_type") or "unknown"
    requirement_info = dep_item.get("requirement_info") or {}
    attempted = attempted_versions_for(name, dependency_attempts)
    constraint_record = build_constraint_record(dep_item, requested_by)

    locked = dict(resolved_versions.get(name) or {})
    locked_version = str(locked.get("version") or "")
    if locked_version:
        if satisfies_constraint(locked_version, constraint, constraint_type, requirement_info):
            return {
                "name": name,
                "constraint": constraint,
                "constraint_type": constraint_type,
                "selected_strategy": "reuse_locked_version",
                "locked_version": locked_version,
                "requested_by": requested_by,
                "constraint_record": constraint_record,
                "conflict": False,
                "candidates": [locked_version],
                "reason": "locked version satisfies current constraint",
            }
        return {
            "name": name,
            "constraint": constraint,
            "constraint_type": constraint_type,
            "selected_strategy": "locked_version_conflict",
            "locked_version": locked_version,
            "requested_by": requested_by,
            "constraint_record": constraint_record,
            "conflict": True,
            "candidates": [],
            "reason": f"locked version {locked_version} does not satisfy constraint {constraint or '<none>'}",
        }

    if constraint_type == "exact":
        version = normalize_version((requirement_info or {}).get("exact_version") or "")
        candidates = [version] if version and version not in attempted else []
        return {
            "name": name,
            "constraint": constraint,
            "constraint_type": constraint_type,
            "selected_strategy": "exact_version",
            "locked_version": "",
            "requested_by": requested_by,
            "constraint_record": constraint_record,
            "conflict": False,
            "candidates": candidates,
            "reason": "exact version derived from dependency constraint",
        }

    if dep_item.get("type") == "python" and constraint_type in {"range", "unbounded"}:
        versions = list_pypi_stable_versions(name)
        filtered = [
            version
            for version in versions
            if version not in attempted and satisfies_constraint(version, constraint, constraint_type, requirement_info)
        ]
        return {
            "name": name,
            "constraint": constraint,
            "constraint_type": constraint_type,
            "selected_strategy": "range_latest_compatible" if constraint_type == "range" else "stable_candidates",
            "locked_version": "",
            "requested_by": requested_by,
            "constraint_record": constraint_record,
            "conflict": False,
            "candidates": filtered[:MAX_CANDIDATES],
            "reason": "generated from compatible stable releases",
        }

    if dep_item.get("type") in ("nodejs", "runtime") and constraint_type in {"range", "unbounded"}:
        versions = list_npm_stable_versions(name)
        filtered = [
            version
            for version in versions
            if version not in attempted and satisfies_constraint(version, constraint, constraint_type, requirement_info)
        ]
        return {
            "name": name,
            "constraint": constraint,
            "constraint_type": constraint_type,
            "selected_strategy": "range_latest_compatible" if constraint_type == "range" else "stable_candidates",
            "locked_version": "",
            "requested_by": requested_by,
            "constraint_record": constraint_record,
            "conflict": False,
            "candidates": filtered[:MAX_CANDIDATES],
            "reason": "generated from compatible npm stable releases",
        }

    return {
        "name": name,
        "constraint": constraint,
        "constraint_type": constraint_type,
        "selected_strategy": "unsupported_candidate_generation",
        "locked_version": "",
        "requested_by": requested_by,
        "constraint_record": constraint_record,
        "conflict": constraint_type in {"range", "unknown"},
        "candidates": [],
        "reason": "no reliable candidate generation strategy for this language/constraint yet",
    }



def build_layer_plan(
    requests: list[dict[str, Any]],
    build_state_dir: str,
    requested_by: str,
    pkgname: str = "",
    lang: str = "",
) -> dict[str, Any]:
    planned: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    planning_log: list[dict[str, Any]] = []

    for dep_item in requests:
        if dep_item.get("conflict"):
            blocked_node = {
                **dep_item,
                "selected_strategy": "prechecked_conflict",
                "locked_version": "",
                "candidates": [],
                "reason": dep_item.get("conflict_reason", "dependency request conflict"),
                "node_state": "blocked",
            }
            blocked.append(blocked_node)
            planning_log.append(
                {
                    "name": dep_item.get("name") or dep_item.get("dep") or "",
                    "identity": dep_item.get("identity") or dep_item.get("name") or dep_item.get("dep") or "",
                    "input_constraint": dep_item.get("constraint") or dep_item.get("requirement") or "",
                    "input_constraint_type": dep_item.get("constraint_type") or "unknown",
                    "selected_strategy": blocked_node["selected_strategy"],
                    "locked_version": "",
                    "candidates": [],
                    "node_state": "blocked",
                    "reason": blocked_node["reason"],
                }
            )
            continue

        resolution = resolve_candidates(dep_item, build_state_dir, requested_by)
        node = {
            **dep_item,
            **resolution,
            "node_state": "blocked" if resolution.get("conflict") else "planned",
        }
        planning_log.append(
            {
                "name": node.get("name") or node.get("dep") or "",
                "identity": node.get("identity") or node.get("name") or node.get("dep") or "",
                "input_constraint": dep_item.get("constraint") or dep_item.get("requirement") or "",
                "input_constraint_type": dep_item.get("constraint_type") or "unknown",
                "selected_strategy": node.get("selected_strategy", ""),
                "locked_version": node.get("locked_version", ""),
                "candidates": list(node.get("candidates") or []),
                "node_state": node["node_state"],
                "reason": node.get("reason", ""),
            }
        )
        if resolution.get("conflict"):
            blocked.append(node)
        else:
            planned.append(node)

    plan = {
        "pkgname": pkgname,
        "lang": lang,
        "requested_by": requested_by,
        "requests": requests,
        "planned": planned,
        "blocked": blocked,
        "planning_log": planning_log,
        "summary": {
            "request_count": len(requests),
            "planned_count": len(planned),
            "blocked_count": len(blocked),
        },
    }
    append_planning_snapshot(
        build_state_dir,
        {
            "pkgname": pkgname,
            "lang": lang,
            "requested_by": requested_by,
            "planning_log": planning_log,
            "summary": plan["summary"],
        },
    )
    return plan


def resolve_layer_candidates(
    requests: list[dict[str, Any]],
    build_state_dir: str,
    requested_by: str,
    pkgname: str = "",
    lang: str = "",
) -> dict[str, Any]:
    return build_layer_plan(requests, build_state_dir, requested_by, pkgname=pkgname, lang=lang)


def main() -> int:
    parser = argparse.ArgumentParser(description="解析依赖版本候选")
    parser.add_argument("--dependency-json", required=True, help="单个依赖项 JSON 文件路径")
    parser.add_argument("--build-state-dir", default="./build_state")
    parser.add_argument("--requested-by", required=True, help="当前请求该依赖的包名")
    args = parser.parse_args()

    try:
        dep_item = read_json(Path(args.dependency_json))
        result = resolve_candidates(dep_item, args.build_state_dir, args.requested_by)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result.get("conflict") else 2
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
