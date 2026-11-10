# Maestro 菜单启动与当前 VCO 绑定

在已有 Maestro 中选择 **Plug-in → Open VCO ISF Explorer**，打开独立桌面窗口。
这是 Maestro 菜单启动器，外部 Tk 窗口不属于 Virtuoso 内部 dock。

## 使用

1. 从要作为 VCO TB 的 Maestro 窗口打开插件。来源 lib/cell/view 自动带入并显示。
2. 选择这个 VCO 的 History。下拉显示原生 history 名称，内部使用其确切 RDB 路径。
   当前 Maestro 已明确选中的 history 会带入；未选中时不按“最新”猜测。
3. 默认选用自动夹具。选择一颗 core MOS 和频偏即可。已有参考 TB 或需要 thermal 时，
   切换到手动 TB 并选择它的 History。多个 Test 或信号不能唯一确定时，按界面提示选择。
4. 点击 **开始分析** 完成预检并启动本次计算。打开菜单、刷新或选择 History 只读取来源。

VCO 路径仍可手动更换；更换后清除旧 history、映射和绑定。
**恢复启动的 Maestro** 回到本程序实例最初的来源。**刷新 History** 重新查询同一来源，
保留当前选择；原历史删除、损坏或未完成时，不悄悄切换到其他记录。

## 安装与环境

菜单文件为 `cadence/vco_isf_menu.il`，启动器为 `cadence/vco_isf_launch`。
在项目 `.cdsinit` 中加载菜单文件。迁移路径时，可在 `load` 前设置 `isfMaestroLauncher`。
启动器从项目虚拟环境、当前环境、PATH 和常见虚拟环境目录检测已有 Python 3.11+ 与 Tcl/Tk，优先使用带 NumPy 的候选。`VCO_ISF_PYTHON` 可显式指定解释器，检测失败会报告该设置。
资源位置按启动器自身目录解析。菜单/源码必须可被实际桌面用户读取和执行。

现有 Plug-in 菜单支持 `maestroPluginItemFactories` 扩展时，ISF 只登记自己的菜单项；
缺少该接口时，使用独立 **VCO ISF** 菜单。每个新 ADE 窗口都创建自己的菜单对象。
项目加载项在新 Virtuoso 会话中自动生效。已经运行的旧会话须先加载更新后的菜单文件，再新开 Maestro 窗口；已有外部 UI 也不会自动载入新 Python 代码。

每次菜单启动创建独立上下文和工作区，默认位于
`~/.vco_isf_explorer/maestro_sessions/session-*`。已成功预检的分析偏好按项目及来源保存。
MOS 和信号覆盖仅在明确选择相同 History、双指纹匹配并重新检查时复用。
普通命令行启动支持原有 `--workspace`。每个活动程序需要独立任务目录。

Python 进程不继承 Cadence 的动态库或 Python/Tk 路径设置。
当前 Maestro 的 Ocean 路径、工作目录、PATH 和许可证环境由上下文保留，
只有原生 Ocean 刷新子进程使用这些设置，不改系统环境或 EDA 安装。

桌面中的“环境检测”和 `vco-isf --diagnose` 显示 Python、Tcl/Tk、NumPy、工作区权限和 Cadence 工具路径。源码版可用 `python3.11 run.py --diagnose --json` 导出同一份检查。工具按手动覆盖、当前 Maestro、Cadence 环境脚本、PATH 和安装目录线索查找。只读诊断仅读取脚本中的字面 PATH 线索，工具启动与许可证签出需要在正常执行流程中验证。

## 绑定与结果读取合同

- 点击时调用当前窗口 session，取得 library/cell/view、实际 Maestro 目录与原生 history 索引。
- RDB 路径与 history 的结果目录分开保存。外置结果或较早的明确 history 不受默认目录和最近16条发现限制。
- 菜单记录只提供准确位置，不能证明仿真完成。原有 RDB、实际 PSF、Spectre 终态、网表、
  参数、模型、信号、单位和哈希校验继续执行。
- 当前 setup 与所选旧 history 分开处理；不使用当前界面参数替换旧仿真工况。
- history 刷新原生只读打开同一个 setup；失败时保留已有来源记录并报错，不全盘搜索其他库。
- UI 在工作线程读取数据，只在主线程更新控件；路径/history 改变会丢弃过期回调。
- 自动模式使用所选 VCO 工况的模型和器件参数生成私有 flicker 夹具。手动模式使用明确绑定的独立 noise TB。

启动协议 `ISF_MAESTRO_CONTEXT_1` 的 C/H/R/E 行采用 SKILL `%L` 字符串。
Python 有界解析，不使用 eval 或 shell 展开，成功消费后删除本次临时上下文文件。
`--maestro-context-file` 只属于桌面入口；兼容 Web UI 仍使用原来的手动绑定方式。

## 回滚

从项目 `.cdsinit` 移除本插件的 load 或恢复本次备份，保留其他插件项。
若恢复本机现有 Plug-in builder，应使用其独立备份，不覆盖 Z11 的其他功能。
关闭本次无活动任务的外部 UI 后即可停止使用新入口；库、历史和 PSF 无需恢复。
涉及运行任务的窗口仍遵守原来的关闭保护。
