#!/usr/bin/env python3
"""
copr API 客户端：供 build-rpm skill 调用，替代本地 rpmbuild。

主要功能：
  - 上传 SRPM 到 copr，提交构建
  - 轮询构建状态
  - 拉取构建日志
  - 下载 RPM 产物
"""
import os
import sys
import json
import base64
import subprocess
import urllib.request
import urllib.parse
import time
import urllib.error


class CoprError(Exception):
    pass


def parse_chroots(value):
    """逗号分隔字符串或列表 → chroot 列表（去空白、去重、保持顺序）。"""
    if not value:
        return []
    items = value.split(",") if isinstance(value, str) else [
        part for v in value for part in str(v).split(",")
    ]
    seen, out = set(), []
    for c in items:
        c = c.strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def primary_chroot(chroots):
    """主 chroot：排序后第一个以 -x86_64 结尾的，否则排序后第一个。"""
    if not chroots:
        return ""
    ordered = sorted(chroots)
    for c in ordered:
        if c.endswith("-x86_64"):
            return c
    return ordered[0]


class CoprClient:
    def __init__(self, config_path=None):
        """
        config_path: copr_config.json 路径，默认读同目录
        也可通过环境变量覆盖：
          COPR_FRONTEND_URL, COPR_API_TOKEN, COPR_API_LOGIN
          COPR_OWNER, COPR_PROJECT, COPR_CHROOT（主 chroot）
          COPR_CHROOTS / COPR_BUILD_CHROOTS 由 build_with_copr 解析
        """
        config = {}
        if config_path and os.path.exists(config_path):
            with open(config_path) as f:
                config = json.load(f)

        # 优先用环境变量，其次用配置文件
        self.frontend_url = (os.environ.get("COPR_FRONTEND_URL")
                             or config.get("copr_frontend_url", "http://localhost:5000")).rstrip("/")
        self.api_token = os.environ.get("COPR_API_TOKEN") or config.get("copr_api_token", "")
        self.api_login = os.environ.get("COPR_API_LOGIN") or config.get("copr_api_login", "")
        self.owner = os.environ.get("COPR_OWNER") or config.get("copr_owner", "")
        self.project = os.environ.get("COPR_PROJECT") or config.get("copr_project", "")
        self.default_chroot = os.environ.get("COPR_CHROOT") or config.get("default_chroot", "")

    def _auth_header(self):
        creds = f"{self.api_login}:{self.api_token}"
        b64 = base64.b64encode(creds.encode()).decode()
        return {"Authorization": f"Basic {b64}"}

    def _get(self, path, params=None):
        url = f"{self.frontend_url}/api_3{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._auth_header())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise CoprError(f"GET {path} failed [{e.code}]: {body}")

    def _post_multipart(self, path, fields, files=None):
        """发送 multipart/form-data POST 请求"""
        boundary = "----CoprBoundary" + str(int(time.time()))
        body_parts = []

        for key, val in fields.items():
            if isinstance(val, list):
                for v in val:
                    body_parts.append(
                        f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{v}\r\n'.encode()
                    )
            else:
                body_parts.append(
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{val}\r\n'.encode()
                )

        if files:
            for field_name, (filename, data) in files.items():
                body_parts.append(
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                    + data + b'\r\n'
                )

        body_parts.append(f'--{boundary}--\r\n'.encode())
        body = b''.join(body_parts)

        url = f"{self.frontend_url}/api_3{path}"
        headers = self._auth_header()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise CoprError(f"POST {path} failed [{e.code}]: {body}")

    def find_active_build(self, pkg_name, chroot=None):
        """
        查找同名包是否有进行中的构建（starting/running/pending）。
        返回 build_id 或 None。
        """
        active_states = {"starting", "running", "pending", "waiting", "forked"}
        try:
            result = self._get("/build/list/", params={
                "ownername": self.owner,
                "projectname": self.project,
                "limit": 20,
            })
            for build in result.get("items", []):
                state = build.get("state", "")
                name = build.get("source_package", {}).get("name", "")
                if state in active_states and name == pkg_name:
                    build_id = build.get("id")
                    print(f"[copr] 发现进行中的构建 build_id={build_id} state={state}，复用")
                    return build_id
        except Exception:
            pass
        return None

    def build_srpm(self, srpm_path, chroots=None, timeout=7200):
        """
        上传 SRPM 并提交构建。返回 build_id。
        """
        if chroots is None:
            chroots = [self.default_chroot] if self.default_chroot else []

        if not chroots:
            raise CoprError("no chroot specified: set COPR_CHROOT or pass chroots parameter")

        print(f"[copr] 上传 SRPM: {os.path.basename(srpm_path)}")
        with open(srpm_path, "rb") as f:
            srpm_data = f.read()

        fields = {
            "ownername": self.owner,
            "projectname": self.project,
            "chroots": chroots,
            "timeout": str(timeout),
        }
        files = {
            "pkgs": (os.path.basename(srpm_path), srpm_data),
        }

        result = self._post_multipart("/build/create/upload", fields, files)
        build_id = result.get("id")
        if not build_id:
            raise CoprError(f"提交构建失败: {result}")

        print(f"[copr] 构建已提交，build_id={build_id}")
        print(f"[copr] 查看进度: {self.frontend_url}/build/{build_id}/")
        return build_id

    def build_srpm_url(self, srpm_url, chroots=None, timeout=7200):
        """
        通过 URL 提交构建（SRPM 必须可被 copr 访问到）。
        返回 build_id。
        """
        if chroots is None:
            chroots = [self.default_chroot] if self.default_chroot else []

        if not chroots:
            raise CoprError("no chroot specified: set COPR_CHROOT or pass chroots parameter")

        print(f"[copr] 提交 SRPM URL: {srpm_url}")
        fields = {
            "ownername": self.owner,
            "projectname": self.project,
            "pkgs": srpm_url,
            "chroots": chroots,
            "timeout": str(timeout),
        }

        result = self._post_multipart("/build/create/url", fields)
        # url 方式返回 {"items": [...], "meta": {}}
        if "items" in result:
            build_id = result["items"][0].get("id") if result["items"] else None
        else:
            build_id = result.get("id")

        if not build_id:
            raise CoprError(f"提交构建失败: {result}")

        print(f"[copr] 构建已提交，build_id={build_id}")
        return build_id

    def get_build_status(self, build_id):
        """返回构建状态字符串：waiting/forked/running/succeeded/failed/canceled/skipped"""
        result = self._get(f"/build/{build_id}/")
        return result.get("state", "unknown")

    def get_build_chroots(self, build_id):
        """返回各 chroot 的构建状态列表"""
        result = self._get("/build/list/", params={
            "ownername": self.owner,
            "projectname": self.project,
        })
        # 直接查单个 build
        build = self._get(f"/build/{build_id}/")
        return build.get("chroots", {})

    def get_build_log(self, build_id, chroot=None):
        """
        拉取构建日志文本。
        返回日志字符串，失败时返回错误信息。
        """
        if chroot is None:
            chroot = self.default_chroot

        # 构建日志 URL 格式
        log_url = (f"{self.frontend_url.replace(':31211', ':5002').replace('36.133.229.59', 'copr-backend')}"
                   f"/results/{self.owner}/{self.project}/{chroot}/{build_id:08d}-*/build.log")

        # 先通过 API 获取 result_dir_url
        try:
            build_info = self._get(f"/build/{build_id}/")
            result_url = build_info.get("result_url", "")
            if result_url:
                log_url = result_url.rstrip("/") + "/build.log"
        except Exception:
            pass

        print(f"[copr] 拉取构建日志: {log_url}")
        try:
            with urllib.request.urlopen(log_url, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            return f"[无法获取日志: {e}]"

    def get_build_log_from_backend(self, build_id, chroot=None):
        """
        通过 backend HTTP 服务器直接获取日志。
        适用于集群内访问。
        """
        if chroot is None:
            chroot = self.default_chroot

        backend_url = "http://copr-backend:5002"
        dir_url = f"{backend_url}/results/{self.owner}/{self.project}/{chroot}/"
        build_prefix = f"{build_id:08d}-"

        try:
            with urllib.request.urlopen(dir_url, timeout=10) as resp:
                content = resp.read().decode()
            import re, gzip
            dirs = re.findall(rf'href="({build_prefix}[^"]+/)"', content)
            if dirs:
                build_dir = dir_url + dirs[0]
                logs = []

                # 拉 builder-live.log（binary 构建日志，主要错误在这里）
                for log_name in ("build.log", "builder-live.log"):
                    try:
                        with urllib.request.urlopen(build_dir + log_name, timeout=30) as resp:
                            logs.append(f"=== {log_name} ===\n" + resp.read().decode("utf-8", errors="replace"))
                            break
                    except Exception:
                        pass
                if not logs:
                    for log_name in ("builder-live.log.gz",):
                        try:
                            with urllib.request.urlopen(build_dir + log_name, timeout=30) as resp:
                                logs.append(f"=== builder-live.log ===\n" + gzip.decompress(resp.read()).decode("utf-8", errors="replace"))
                                break
                        except Exception:
                            pass

                # 始终追加 backend.log（包含 COPR 侧错误如 results.json not found）
                for log_name in ("backend.log", "backend.log.gz"):
                    try:
                        with urllib.request.urlopen(build_dir + log_name, timeout=30) as resp:
                            raw = resp.read()
                            text = gzip.decompress(raw).decode("utf-8", errors="replace") if log_name.endswith(".gz") else raw.decode("utf-8", errors="replace")
                            logs.append(f"=== backend.log ===\n" + text)
                            break
                    except Exception:
                        pass

                if logs:
                    return "\n\n".join(logs)
        except Exception:
            pass

        return f"[无法获取后端日志]"

    def get_result_dir(self, build_id, chroot=None):
        """返回构建产物目录 URL"""
        if chroot is None:
            chroot = self.default_chroot
        backend_url = "http://copr-backend:5002"
        build_prefix = f"{build_id:08d}-"
        dir_url = f"{backend_url}/results/{self.owner}/{self.project}/{chroot}/"
        try:
            import re
            with urllib.request.urlopen(dir_url, timeout=10) as resp:
                content = resp.read().decode()
            dirs = re.findall(rf'href="({build_prefix}[^"]+/)"', content)
            if dirs:
                return dir_url + dirs[0]
        except Exception:
            pass
        return None

    def download_rpms(self, build_id, dest_dir, chroot=None):
        """
        从 copr backend 下载 RPM 产物到本地目录。
        返回下载的文件路径列表。
        """
        if chroot is None:
            chroot = self.default_chroot

        os.makedirs(dest_dir, exist_ok=True)
        result_dir = self.get_result_dir(build_id, chroot)
        if not result_dir:
            raise CoprError(f"找不到构建 {build_id} 的产物目录")

        print(f"[copr] 下载 RPM 产物从: {result_dir}")
        import re
        with urllib.request.urlopen(result_dir, timeout=10) as resp:
            content = resp.read().decode()

        rpm_files = re.findall(r'href="([^"]+\.rpm)"', content)
        downloaded = []
        for rpm in rpm_files:
            if rpm.startswith(".."):
                continue
            url = result_dir + rpm
            dest = os.path.join(dest_dir, rpm)
            print(f"[copr]   下载: {rpm}")
            urllib.request.urlretrieve(url, dest)
            downloaded.append(dest)

        print(f"[copr] 共下载 {len(downloaded)} 个 RPM")
        return downloaded


def build_with_copr(srpm_path, config_path, output_json, chroot=None, chroots=None):
    """
    提交构建到 COPR，立即返回（不等待）。
    结果写入 output_json：status=copr_running, copr_build_id=<id>
    构建完成后由 job_runner 的 wait loop 负责拉取日志和写最终状态。

    chroot 解析优先级：chroots 参数（列表或逗号分隔）> chroot 单值参数
    > 环境变量 COPR_BUILD_CHROOTS > COPR_CHROOTS > COPR_CHROOT/config default_chroot。
    单 chroot 场景（单元素列表）行为与旧版一致。
    """
    client = CoprClient(config_path)
    chroot_list = parse_chroots(chroots) or parse_chroots(chroot)
    if not chroot_list:
        chroot_list = (parse_chroots(os.environ.get("COPR_BUILD_CHROOTS"))
                       or parse_chroots(os.environ.get("COPR_CHROOTS")))
    if chroot_list:
        client.default_chroot = primary_chroot(chroot_list)
    else:
        # 旧回退：由 build_srpm 使用 default_chroot（COPR_CHROOT / config）
        chroot_list = parse_chroots(client.default_chroot)

    result = {
        "status": "failed",
        "copr_build_id": None,
        "copr_chroot": client.default_chroot,
        "copr_chroots": chroot_list,
        "copr_build_ids": {},
        "build_log": "",
        "rpms": [],
        "failure_reason": "",
    }

    try:
        build_id = client.build_srpm(srpm_path, chroots=chroot_list or None)
        result["copr_build_id"] = build_id
        # 一次提交 = 一个 build × N 个 build_chroot，各 chroot 共享同一 build_id
        result["copr_build_ids"] = {c: build_id for c in chroot_list}
        result["status"] = "copr_running"
    except CoprError as e:
        result["failure_reason"] = str(e)

    with open(output_json, "w") as f:
        json.dump(result, f, indent=2)

    print(f"[copr] 结果已写入: {output_json}")
    return result["status"] == "copr_running"


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="copr 构建客户端 — 提交 SRPM 并立即返回")
    parser.add_argument("srpm", help="SRPM 文件路径")
    parser.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts", "copr_config.json"))
    parser.add_argument("--output", default="build_result.json")
    parser.add_argument("--chroot", default=None,
                        help="单个目标 chroot（等价于 --chroots 的单元素形式，兼容旧调用）")
    parser.add_argument("--chroots", default=None,
                        help="目标 chroot 列表，逗号分隔（如 openeuler-24.03-x86_64,openeuler-24.03-aarch64）")
    args = parser.parse_args()

    success = build_with_copr(args.srpm, args.config, args.output,
                              chroot=args.chroot, chroots=args.chroots)
    sys.exit(0 if success else 1)
