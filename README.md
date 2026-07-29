# DualSPHysics Studio 1.0.1

面向 DualSPHysics v5.4 的 Windows XML 可视化编辑器与本地计算流程前端。

Copyright © 2026 Fang Li。项目采用 [MIT License](LICENSE) 发布。

## 主要能力

- XML 源码、结构树、可搜索参数工作台和 3D 场景联动；现有标量参数可通过无损补丁安全编辑。
- `drawbox`、`drawprism` 的轻量表面预览、逐顶点拖动、旋转、缩放与精确坐标编辑；单独移动 `drawbox` 顶点时会转换为 8 点 `drawprism`，并以 XML 注释保留原命令。
- 加载外部 3D 文件时仅支持 `.vtk` 和 `.stl`；读取前执行文件、点和单元预算预检，大型模型需确认，超过硬上限则阻止加载。
- “算例医生”汇总并筛选 XML 结构、计算域、marker、参数生效状态和时间配置问题；双击问题可在独立详情页查看完整描述、XML 路径、行号和源码上下文，双击位置仍可直接跳转到对应源码行。
- 直接调用已有 `GenCase_win64.exe`、`DualSPHysics5.4CPU_win64.exe`、`DualSPHysics5.4_win64.exe` 和 `PartVTK_win64.exe`，依次生成初始模型、CPU/GPU 求解数据和粒子 VTK。
- “拓展功能”菜单按需发现便携目录 `DLC` 中由用户单独放入的独立 EXE，只有用户点击工具后才直接启动。
- 可选 OpenAI Responses API 中文解读；离线功能不需要密钥或网络。

## 从源码运行

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m dualsphysics_studio
```

首次运行时可通过“运行 → 配置 DualSPHysics 路径…”选择 v5.4 根目录，也可预先设置 `DUALSPHYSICS_ROOT`。程序会严格核验 GenCase、`DsphConfig.xml`、CPU/GPU 求解器和 PartVTK 后再保存路径；不会从案例目录或当前工作目录自动选择可执行文件。该用户设置不会写入发布包。程序不会随包分发或修改 DualSPHysics 官方工具。
本项目是独立前端，与 DualSPHysics 项目及其开发团队不存在隶属、赞助或官方认可关系。

## 构建 Windows x64 便携包

使用 64 位 Python 3.13，在 Windows PowerShell 中运行：

```powershell
.\packaging\build_portable.ps1 -PythonExe C:\path\to\python.exe
```

若当前环境已安装 `requirements-lock.txt` 中的固定版本，可添加 `-SkipInstall`。构建脚本使用 PyInstaller `onedir`，只生成目录，不额外创建 ZIP：

- `outputs\DualSPHysics Studio-v1.0.1 (windows x64)\`

便携目录包含中文快速说明和合并后的第三方许可证，不包含 Python `*.dist-info` 安装元数据、DualSPHysics 官方二进制或任何案例文件。其基本结构为：

```text
DualSPHysics Studio-v1.0.1 (windows x64)\
  DualSPHysicsStudio.exe
  _internal\
  快速说明.md
  LICENSE
  THIRD_PARTY_LICENSES.txt     # 第三方清单、声明和完整许可证原文
  DLC\                         # 默认拓展功能扫描目录
    README.txt
```

`outputs` 被 Git 忽略，避免把数百 MB 的构建结果提交到源码历史。GitHub Release 不能直接上传目录；若将来需要一个可下载的二进制资产，应在发布流程中单独归档，而不是由本地构建脚本重复生成 ZIP。

扩展工具不随 Studio 本体发布。单文件工具可直接放入 `DLC`；带 DLL、插件或许可文件的便携工具应完整放在 `DLC` 的一级子目录中。

## 安全说明

- XML 解析禁用 DTD、外部实体和网络访问。
- XML 文件在完整读取前执行 80 MiB 字节上限检查，并继续执行字符数、元素数和嵌套深度限制。
- STL/legacy VTK 在交给 VTK 读取器前执行有界预检；512 MiB、500 万点/单元或约 2 GiB 内存估算触发确认，4 GiB 或 2500 万点/单元触发硬阻断。
- GenCase、CPU/GPU 求解器和 PartVTK 均以参数数组直接启动，不经过 BAT 或命令行 shell。
- GenCase、CPU/GPU 求解器和 PartVTK 仅从已保存设置、`DUALSPHYSICS_ROOT` 或用户明确选择的目录解析。
- `DLC` 工具仅在用户点击后以原 EXE 直接启动，不经过 shell/BAT，也不自动附加参数。
- 已存在求解数据或目标 VTK 时阻止运行，不自动删除或覆盖旧结果。
- 保存前自动创建源 XML 备份。
- AI 默认关闭；启用后会在发送前展示载荷，建议不会自动应用或运行。

完整操作说明见 [快速说明.md](快速说明.md)；便携包用户也直接打开根目录的同名文件。

## 开源与第三方软件

DualSPHysics Studio 自有源代码采用 MIT License。
便携包中的 Qt for Python、VTK、Python 运行时和其他依赖仍分别遵循其上游许可证，
发布构建会把 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)、标准许可文本及各依赖的原始声明合并为一个 `THIRD_PARTY_LICENSES.txt`。公开发布 Qt/PySide 二进制时，[GitHub Release](https://github.com/Zerliner/DualSPHysicsStudio/releases) 同时提供对应源码归档和 SHA-256 校验清单。

GitHub 项目地址：https://github.com/Zerliner/DualSPHysicsStudio

发布后的开发优先级与稳定性约束见 [ROADMAP.md](ROADMAP.md)。
