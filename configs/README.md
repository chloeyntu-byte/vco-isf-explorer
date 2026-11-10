# Automatic setup and expert configuration templates

Normal Tk and Web use does not require creating an engine, profile, or JSON
file.  Select the two Maestro TBs, inspect their generated histories, and use
**Advanced settings → Automatic detection and manual overrides** to review
or correct the detected names and execution settings. The default mapping
view shows the signal roles actually used by the current run, plus unresolved
or invalid choices. The complete inventory and other execution details stay
available in folded diagnostic views. Each editable row
shows its purpose, evidence/source, automatic value, candidates, manual
override, and effective value.  Clearing an override restores automatic
detection; ambiguous candidates require an explicit selection and re-check.

VGS, VDS, and VBS are semantic roles.  The VCO waveform/output name and the
flicker DC variable name can differ; their links are shown in the cross-TB
table.  Nets, outputs and expressions, variables, devices, analyses, and other
discovered names are available in each TB's full catalog as dropdown candidates
or diagnostic evidence; they do not all need to appear in the normal mapping
view. These are name
selections for the tool, not requests to rename the original Maestro objects.

Device connectivity is read from the generated netlist of the selected
history, not directly from an OA schematic binary.  Missing named waveforms
can be derived from confirmed device terminals where supported.  Complex
Calculator expressions are listed as reference evidence and never evaluated;
an unsupported expression selected as an execution waveform must be replaced
with a supported waveform or cleared so confirmed terminals can be used.

The UI stores intentional name choices in each role's `mapping_overrides` and
execution choices in `portable_setup.overrides`.  Detection snapshots are not
manual overrides.  Switching TBs clears old pair-specific settings with a
visible notice; edited or missing names require fresh inspection and preflight.

## 普通使用：只核对真正会用到的名称

1. 选择两个 TB 和各自完整 history，点击检测与绑定。
2. 在高级设置核对输出正端、输出负端、传递幅相系列、目标器件，以及跨 TB 的
   VGS/VDS/VBS 对应。默认值取自本次所选 history，不要求名字与示例一致。
3. 名字不对时从对应用途的下拉候选中选择，再“应用并验证”。清空覆盖或选择
   “恢复自动”可回到自动识别。错误、过期或有歧义的对应不会被隐藏。
4. 只有要追查来源时才展开“查看所有实际名称”或“显示未使用映射和执行细节
   （诊断）”。这里不是要求用户逐个填满所有 net/output/variable。

`mag_pxf0` 只是可能存在的名称，不是固定要求。可选择 `mag_pxf` / `ph_pxf`
或 `harm_mag` / `harm_ph` 系列，也兼容单项输出。工具解析原始传递来源后，
按当前所选差分输出重新测量完整复数响应；原有标量不直接代替算法输入。
电流注入的物理含义由用户保证，不再额外强制同漏源支路或幅相同源校验。
历史已明确的反向支路保留符号换算，其余按声明方向使用。没有合适命名输出时，
可选用明示的私有漏源注入回退；不能读取的手动选择会报错，不会偷偷换源。
振荡基波幅相由相同差分 HB 输出取得，不需要单独配置 `VH1/ph`。

像 `HB_PN_VCO_10k`、`noise` 这样的点噪输出名可供对照，但内置路径读取原始
噪声数据，不执行任意 Calculator 表达式，也不要求为每个频偏建立一个新 output。
VCO 的 `HB_TIME_VGS` 与噪声 TB 的 `VGS` 可以名称不同；是否对应依据栅源电压
等电气含义，而不是字符串相同。

## 频偏和工况

在第一页的分析目标选“多个单点”“线性”或“对数”，不需要修改配置文件。
例如 `1k,10k,1M,100M`；裸数字为 Hz，`k` 为千，`M` 为百万，小写 `m` 不接受。

- 线性步长：起点 `1k`、终点 `10k`、步长 `4k` 得到 1、5、9、10 kHz；不能整除
  时追加终点。
- 线性间隔数：N 个间隔产生 N+1 点。例如相同起止点、3 个间隔得到 1、4、7、10 kHz。
- 对数：`1k` 至 `100M`、每 decade 1 点得到 1k、10k、100k、1M、10M、100M。
  非整 decade 的终点也会包含。

所有频偏必须为有限正数，展开后按 Hz 排序去重，最多 256 点。预览显示实际
点表和“工况数 × 频偏数”；这是待运行样本数，不是更高的签核工况数。
每个频偏执行完整标量流程，不从已计算相噪插值。单频偏保持原计算路径。

ADE 逐点参数会从所选运行记录恢复到私有网表，所选 test 的有效值优先于同点
公共值；同优先级冲突、未求值表达式、实例局部覆盖或不能唯一定位的声明会
拦截，不会静默忽略。噪声 TB 的偏置扫描维度与非偏置工况分开处理，并核对
温度及共用模型 section。结果保留实际参数快照，原 TB 和历史网表不被修改。

多频偏可“当前频偏完成后停止”并查看已完成部分。恢复会验证原输入与结果，
复用已完成的频偏；修改 TB、模型或执行文件后通常需要新任务，不能混入旧任务。

## Optional expert files

Every file in this directory is intentionally fictitious.  Names such as
`PDK_A`, `device_model.scs`, and `generic_nmos` do not describe a foundry kit
or customer design.

If maintaining a custom execution integration, copy the closest template
outside the source checkout, then replace model,
topology, hierarchy, terminal, trace, and executable values with values that
you are authorized to use.  Do not commit a filled private profile to a public
repository.

For this expert-file workflow, copy the engine JSON and its physical profile
as a pair.  A relative `physical_profile_path` is resolved from the engine JSON's
directory; alternatively, place the `physical_profile` object directly in the
engine JSON.  Update both files after moving them to a different host or PDK.

The default path constructs an inspectable execution contract from the bound
history's device hierarchy, four-terminal nodes, models, probes, and selected
signals.  An imported engine/profile supplies that contract instead.  Under
**Advanced settings → Expert file import**, a non-empty engine path overrides
the automatic execution setup.  Its generated suggestions are explicitly
inactive/read-only; clear the expert path to edit and use automatic settings.
Compatibility field names mean:

- `config_path` selects an optional expert engine JSON;
- `physical_profile` / `physical_profile_path` describe the inline or linked
  device/node/probe contract within that expert file; and
- `execution.allow_execute` is request-scoped execution authority. The normal
  UI supplies it when the user explicitly requests preflight/start, not via a
  saved checkbox; save/load/binding does not grant it.

Legacy template matching with `pair_match` remains available for integrations.
It does not prove physical correctness and is not required for the normal
automatically generated setup.  The shipped fictitious templates normally do
not match a private design and must not be run unchanged against one.

`max_harmonic` must be between 1 and 12.  Phase-sign selection enumerates
`2^N` candidates, so preflight and both execution/import backends enforce this
shared safety limit.

For integrations, scalar compatibility remains `analysis.offset.hz`. A
multi-offset request uses `analysis.offsets`; for example:

```json
{
  "analysis": {
    "offsets": {"mode": "list", "values": "1k,10k,1M,100M"}
  }
}
```

`values_hz`, if returned in a resolved configuration, is derived preview data,
not an input override. The service expands the recipe and creates isolated
scalar requests. Do not hand-build a scalar CSV containing several offsets
under the same `point`. See [the format contract](../formats/README.md) for
linear/log recipes, result identities and portable aggregate exports.
