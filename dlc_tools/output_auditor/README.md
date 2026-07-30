# DSS 结果完整性审计器

这是与 DualSPHysics Studio 分开分发的只读 DLC 工具。它检查结果目录的文件级完整性，不解析专有 BI4 内容，也不会修改、删除或重命名任何案例文件。

## 功能

- 统计 VTK、VTU、BI4、CSV、XML、日志等文件的数量和磁盘占用。
- 识别 `Part*`、`PartAll*` 等连续编号序列并报告缺帧。
- 报告零字节文件和达到扫描安全上限的截断清单。
- 读取输出根目录中的 `.dss-run/run.json`，提示失败、中断或取消的阶段。
- 将完整结果导出为 UTF-8 JSON。

## 安装

保持发布 ZIP 的目录结构，将整个 `DSS Output Auditor` 文件夹放入 Studio 便携目录旁的 `DLC` 文件夹。Studio 会读取 `dss-extension.json` 并在“拓展功能”菜单显示版本、发布者、许可和支持的输入类型。

首次启动时 Studio 会显示信任确认。工具不会由 Studio 自动运行，也不会自动接收当前案例路径。

`LICENSE` 适用于审计器自有源码；便携包中的
`THIRD_PARTY_LICENSES.txt` 保留其 Python、Tcl/Tk 及随 Python Windows
运行时分发组件的许可与声明。

## 限制

- 检查是文件清单级别的，不验证 VTK 网格语义或 BI4 内部数据。
- 默认最多扫描 250,000 个普通文件，且不会跟随目录符号链接。
- 缺帧只在同一目录、同一前缀和同一扩展名的最小/最大编号之间判断。
