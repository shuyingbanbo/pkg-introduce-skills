# Fix Reason: openEuler Library Path Specific

**原因 / Reason:**
openEuler 系统上，特定架构（如 x86_64 或 aarch64）的系统库路径为 `/usr/lib64`。而上游构建脚本在寻找或指定系统库时出现了兼容性问题。

**修改内容 / Modification:**
强行指定了 `libyaml-cpp.so` 的绝对路径 `/usr/lib64/libyaml-cpp.so...`，以解决链接报错问题。

**注意事项 / Notice:**
这种绝对路径的硬编码写法缺乏通用性（例如在 32 位系统或其他架构上可能失效），未来升级应尽量通过标准的 `find_package` 或 `pkg-config` 解决。
