# Fix Reason: Upstream Missing Include (GCC compatibility)

**原因 / Reason:**
源码中使用了 `std::map`，但忘记了 `#include <map>`。较老的 GCC 版本可能有隐式包含所以未报错，但在 openEuler 较新的 GCC 环境下，严格的头文件检查会导致编译失败。

**修改内容 / Modification:**
Patch 补充了缺失的 `#include <map>`。

**注意事项 / Notice:**
升级此包前，需检查上游新版本是否已经修复了该问题。如果已修复，应废弃此 Patch。
