"""tests/archive 共享测试工具(非 conftest,测试文件按需显式导入)。

docker_cp_real_run:把 subprocess.run 包装为
  - `docker cp container:/path dst`(容器→本地方向)真实落盘,让 copy_pkg_files /
    sync_rpms 里的集合差值(new_dist_rpms / synced)等文件系统逻辑可被断言;
  - 其余命令原样委托 orig_run(通常为 fake_subprocess 的 run,保留其调用记录)。
"""

import subprocess as _sp
from pathlib import Path


def docker_cp_real_run(orig_run):
    """返回包装后的 subprocess.run 实现。"""

    def run(cmd, **kwargs):
        if (isinstance(cmd, (list, tuple)) and len(cmd) >= 4
                and cmd[0] == "docker" and cmd[1] == "cp"
                and ":" in str(cmd[2])):
            remote_path = str(cmd[2]).split(":", 1)[1]
            dst = Path(cmd[3])
            if dst.is_dir():
                # 目标为目录:按 docker cp 语义写入 dst/<basename>
                (dst / Path(remote_path).name).touch()
            else:
                # 目标为文件路径(如 <pkg>/<pkg>.spec):直接创建
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.touch()
            # 同时把调用记录进 fake(供 called_with 断言)
            fake = getattr(orig_run, "__self__", None)
            if fake is not None and hasattr(fake, "calls"):
                fake.calls.append((cmd, kwargs))
            return _sp.CompletedProcess(cmd, 0, None, None)
        return orig_run(cmd, **kwargs)

    return run
