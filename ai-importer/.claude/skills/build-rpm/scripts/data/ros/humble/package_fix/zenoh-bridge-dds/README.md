# Fix Reason: Rust Cargo Offline Build (OBS Isolation)

**原因 / Reason:**
`zenoh-bridge-dds` 是使用 Rust 编写的包。在编译阶段 (`%build`)，`cargo` 包管理器会尝试连接 `crates.io` 下载海量的依赖包。
由于 openEuler OBS 编译机处于完全断网状态，这会导致 Cargo 编译立即失败。

**修改内容 / Modification:**
1. 使用 `cargo vendor` 命令在有网环境预先下载好所有依赖，并打包成了 `zenoh-bridge-dds-cargo-vendor.tar.gz`。
2. 在 `source.fix` 中引入该离线包作为 `Source1`。
3. 在 `prep.fix` 中执行 `tar -xzf %{SOURCE1}` 将依赖解压到工作空间供离线编译使用。

**注意事项 / Notice:**
未来每次升级 `zenoh-bridge-dds` 时，由于其 Cargo 依赖也会发生变化，必须**重新手动执行 `cargo vendor`** 生成新的 vendor tarball，否则依然会因为缺少新依赖而编译失败！
