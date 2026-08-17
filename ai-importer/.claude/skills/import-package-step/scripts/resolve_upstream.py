#!/usr/bin/env python3
"""
Resolve upstream URL for a dependency without AI.

Queries language-specific registries (npm / PyPI / crates.io) via HTTP API,
validates results against a trusted-host whitelist, and writes back to
dep_registry.json.  Designed to replace most resolve-upstream agent calls.

Exit codes:
  0 — URL resolved and written to dep_registry.json
  1 — resolution failed (needs AI fallback via resolve-upstream agent)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

TRUSTED_HOSTS = [
    "github.com", "gitlab.com", "gitee.com", "gitcode.com",
    "bitbucket.org", "sourceforge.net", "salsa.debian.org",
    "savannah.gnu.org", "codeberg.org", "git.sr.ht",
]


# ── helpers ─────────────────────────────────────────────────────────────────

def _http_get(url: str, timeout: int = 15) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ai-importer/1.0"})
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None


def _is_trusted(url: str) -> bool:
    if not url:
        return False
    return any(h in url for h in TRUSTED_HOSTS)


def _clean_git_url(url: str) -> str:
    """git://…, git+https://…, git://github.com/… → https://…, strip .git suffix"""
    if not url:
        return ""
    # protocol-less: git@github.com:user/repo.git
    if url.startswith("git@"):
        url = "https://" + url.split("@", 1)[1].replace(":", "/", 1)
    # git://, git+https://, git+ssh://
    url = re.sub(r"^git\+", "", url)
    url = re.sub(r"^git://", "https://", url)
    url = re.sub(r"^ssh://git@", "https://", url)
    url = re.sub(r"\.git$", "", url)
    return url.rstrip("/")


# ── per-language resolvers ─────────────────────────────────────────────────

def resolve_nodejs(pkgname: str) -> str | None:
    """Strip nodejs- prefix, query https://registry.npmjs.org/<name>."""
    name = pkgname.removeprefix("nodejs-")
    data = _http_get(f"https://registry.npmjs.org/{name}")
    if not data:
        return None
    repo = data.get("repository") or {}
    if isinstance(repo, dict):
        url = _clean_git_url(repo.get("url", ""))
        if _is_trusted(url):
            return url
    # fallback: homepage
    homepage = _clean_git_url(data.get("homepage", ""))
    if _is_trusted(homepage):
        return homepage
    return None


def resolve_python(pkgname: str) -> str | None:
    """Strip python- / python3- prefix, query https://pypi.org/pypi/<name>/json."""
    name = re.sub(r"^python3?-", "", pkgname)
    data = _http_get(f"https://pypi.org/pypi/{name}/json")
    if not data:
        return None
    info = data.get("info", {})
    # project_urls first
    proj_urls = info.get("project_urls") or {}
    for _key, val in proj_urls.items():
        if isinstance(val, str) and _is_trusted(val):
            return val.rstrip("/")
    # fallback fields
    for field in ("project_url", "home_page", "package_url"):
        val = info.get(field, "")
        if isinstance(val, str) and _is_trusted(val):
            return val.rstrip("/")
    return None


def resolve_rust(pkgname: str) -> str | None:
    """Query https://crates.io/api/v1/crates/<name>."""
    data = _http_get(f"https://crates.io/api/v1/crates/{pkgname}")
    if not data:
        return None
    repo = (data.get("crate") or {}).get("repository", "")
    if repo and _is_trusted(repo):
        return repo.rstrip("/")
    return None


# ── language detection ─────────────────────────────────────────────────────

def detect_lang(pkgname: str, hint: str = "") -> str:
    if hint:
        return hint
    if pkgname.startswith("nodejs-"):
        return "nodejs"
    if re.match(r"^python3?-", pkgname):
        return "python"
    if pkgname.startswith("rust-"):
        return "rust"
    return ""


# ── main resolver ──────────────────────────────────────────────────────────

RESOLVERS = {
    "nodejs": [("npm", resolve_nodejs)],
    "python": [("pypi", resolve_python)],
    "rust":   [("crates.io", resolve_rust)],
}


def resolve_upstream(pkgname: str, lang_hint: str = "") -> tuple[str | None, str | None]:
    """Try to resolve upstream URL. Returns (url, source) or (None, None)."""
    lang = detect_lang(pkgname, lang_hint)
    ordered = RESOLVERS.get(lang, []) + [
        ("npm", resolve_nodejs),
        ("pypi", resolve_python),
    ]
    for source, resolver in ordered:
        url = resolver(pkgname)
        if url:
            return url, source
    return None, None


# ── CLI ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve upstream URL for a dependency without AI"
    )
    parser.add_argument("--pkg", required=True)
    parser.add_argument("--lang", default="", help="Language hint (nodejs|python|rust)")
    parser.add_argument(
        "--session-dir", default=".",
        help="Session directory (writes URL to dep_registry.json)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output JSON instead of plain text",
    )
    args = parser.parse_args()

    url, source = resolve_upstream(args.pkg, args.lang)

    if url:
        reg_path = Path(args.session_dir) / "dep_registry.json"
        if reg_path.exists():
            try:
                reg = json.loads(reg_path.read_text(encoding="utf-8"))
                if args.pkg in reg and not reg[args.pkg].get("url"):
                    reg[args.pkg]["url"] = url
                    reg[args.pkg]["url_resolution"] = source
                    reg_path.write_text(
                        json.dumps(reg, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
            except (OSError, json.JSONDecodeError):
                pass

        if args.json:
            print(json.dumps({"status": "resolved", "url": url, "source": source}))
        else:
            print(url)
        return 0
    else:
        if args.json:
            print(json.dumps({"status": "failed", "reason": "no trusted URL found"}))
        else:
            print("RESOLVE_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
