# Fix Reason: Runtime Dependency Injection

**原因 / Reason:**
`rmw-implementation` 是 ROS 2 的中间件实现层，它在运行时需要动态加载一个具体的 RMW (ROS Middleware) 实现（如 FastRTPS, CycloneDDS 等）。如果打包时不在 RPM 层面强关联一个底层的实现，用户 `dnf install ros-humble-rmw-implementation` 后将无法运行任何 ROS 节点。

**修改内容 / Modification:**
在 `source.fix` 中，通过追加 `Requires` 和 `Recommends` 的方式，硬性地在生成的 `.spec` 文件中注入了对 `rmw-implementation-packages(member)` 和 `rmw-fastrtps-cpp` 的依赖。

**注意事项 / Notice:**
由于 `ros-oe-upstream-init` 目前主要是根据 `package.xml` 来解析依赖，而运行时的特定实现往往不会写死在 `package.xml` 里，因此目前需要通过 `.fix` 来补齐这层发行版特定的依赖关系。
