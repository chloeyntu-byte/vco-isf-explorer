# 双 TB 名称对应与波形传递

## 结果页：总相噪与单频点 Noise contributions

主图与主表的仿真参考是**本次私有 VCO 仿真的总 HBNOISE PM-SSB 相噪**，包含 flicker、热噪声等全部实际仿真噪声源。不是原 Maestro history 的现成 `PN_10k` 标量，也不是噪声副本 TB 的 `noise`；工况来自绑定的 history，程序按本次输出端和频偏重新仿真。

ISF 计算固定采用对称 gm pair：代表侧的独立器件功率翻倍。可选择 flicker、thermal 或两者；thermal 还纳入已支持的原生电阻、电感源项，这些源不再翻倍。不代表任意无源网络或自定义 Verilog-A `thermal_noise` 已受支持。主仿真参考始终为总 HBNoise。查看“Noise contributions”可按工况/频偏对比计算与仿真的同器件、同机制结果。汇总行与源明细重叠，不应重复相加。其他有源器件、shot 或未支持模型不冒充已计算；见 [thermal 方法和范围](THERMAL_NOISE.md)。

总噪声 `out` 是 V/√Hz，直接以相同 HB 差分载波归一化；器件机制分项是 V²/Hz，开方一次后换算。占比使用仿真 PSD / 总 PM-SSB PSD，而不是相加 dB。贡献表与谐波 Qk 表不同：Qk 是同一源各谐波的相干贡献，不能解释成独立源功率占比。

旧结果未保存总噪声和明细时显示“Not collected / N/A”，不会用已有单器件 flicker 冒充总相噪；新任务生成这些额外结果。仿真数值告警或 HBAC 辅助相位检查失败会保留，曲线接近不代表物理验证已经通过。

## 换一套 TB 时怎么操作

1. 分别选择 VCO、噪声 TB 的 Maestro、test 和完整 history。一个 Maestro 有多个 test 时，先选 test；器件选择不能代替 test 选择。MOS 参考作为普通噪声基线时选 `REF_MOS_DC`。
2. 在 Advanced settings 的“Calculation role”和“Actual name”逐行核对。每行可从该 TB **全部同类型候选**中选择，也可直接输入真实名称；不要求名字里包含 VGS、noise 等关键词。`/OUTN` 和 `OUTN` 可按网表别名解析。
3. 修改后点“Apply all & validate”。输入不会被迟到的旧识别结果覆盖。恢复自动只清除该行手动值；换 TB 会清除旧对应，避免静默沿用。
4. 查看“VCO → Noise TB Waveform transfer check”检查。通过说明已验证静态准备条件，**不表示已运行新仿真**。点击明确的环境检查或开始仿真按钮后执行本次操作；不再需要额外 Live/授权勾选。保存、加载、识别绑定本身不会启动仿真。

不存在的名称、不能读取的表达式不会被悄悄替换成 fallback。候选列表提供选择自由；端子波形需要可验证的读取合同，传递输出对应电流注入的物理含义由用户保证。

## 哪些对应实际用于计算

| 用途 | 可选名称类型 | 实际作用 |
| --- | --- | --- |
| VCO 差分输出正端 / 负端 | VCO net | 决定两端相减的极性，以及重新测量的 HBNOISE/HBXF 输出 |
| VCO 目标器件、配对器件 | VCO device | 确定待测 MOS、四端和噪声注入支路 |
| 传递幅值 / 相位系列 | VCO 输出系列或单项 output | 由用户保证输出对应电流注入；解析原始结果来源，本次重新读取完整复数传递函数，不照搬旧 Calculator 数字 |
| VGS / VDS / VBS 波形 output | VCO 命名 output | 检查命名波形是否确实对应目标器件的端子差分；缺项允许从私有 HB 原始端子补算 |
| VGS / VDS / VBS 偏置变量 | 噪声 TB variable | 从相同周期波形抽取准静态偏置点，测量 Mdc 基线 |
| VGS / VDS / VBS 电压源 | 噪声 TB source | 验证偏置变量控制正确端子；DC 私有副本不读取旧 PWL 文件 |
| VGS / VDS / VBS 波形文件变量 | 噪声 TB global variable | 周期副本读取本次运行生成的 PWL 文件；旧 TB 未声明时脚本在私有网表补齐并明示 |
| VS 源极公共电平文件变量 | 噪声 TB global variable / 脚本补齐 | 使源极非地时仍重现真实四端电压；独立于三种端子差分，原 TB 不必新增公共电平源 |
| 噪声目标器件 / 电流检测源 / 电流—电压读出 | 噪声 TB device/source | 选择真实漏极支路、指定器件的 flicker 分量及读出比例；手选检测源可进一步确定对应器件 |

完整候选清单仍可在诊断中展开，但不会把所有未用名称塞进主对应表。噪声总 output 不是自动等于目标器件 flicker；界面会区分仅作对照与真正计算输入。

## 传递幅值与相位按系列选择

`mag_pxfm1、mag_pxf0…mag_pxf6` 可合并为 `mag_pxf`；`harm_mag_m1、harm_mag_0…harm_mag_N` 可合并为 `harm_mag`。相位同理支持 `ph_pxf`、`harm_ph`；基础名称不必包含 pxf、mag 或 ph 等固定关键词。下拉框默认收起该系列的成员，选择后在说明中查看实际成员、结果来源与边带。仍兼容旧单项选择和手动输入，不需要改原 Maestro output 名称。

幅相输出对应电流注入的物理含义由用户保证；程序不再用 MOS 漏源支路、源模型、原测量端点、幅相同源等额外条件反复阻断选择。表达式解析用于取得原始信号和分析等读取信息，不执行任意 Calculator 脚本。名字后缀只是分组线索，不直接代替结果中的边带坐标；不再强制所选单项必须是边带 0。

幅值选择用于定位本次读取的原始复数传递来源；相位选择保留其来源说明，在没有幅值来源时也可定位。实际幅相仍来自本次同一复数响应，不把两个旧标量或不同源的幅相数值拼接。历史上明确反向的注入保留原方向换算；其余按用户声明方向使用，并在来源说明中标注，不增加新的必填校验步骤。

支持 `phaseDegUnwrapped(harmonic(getData(...) 0))` 和 `harmonic(phaseDegUnwrapped(getData(...)) 0)` 两种来源表达式结构。不存在的名称、无法取得读取位置、结果文件/目标频点缺失或非有限数值仍会报告具体问题；这与重复核验用户的电路含义不同。

振荡基波不另设 `VH1/ph` 下拉框。基波幅值与相位始终从当前所选输出正负端口的同一次 HB 复数波形相减取得，与相噪测量口径一致。例如某些 VCO 的原生 `VH1/ph` 为单端 OUTP，不直接替代本次差分基波。

预检遇到一处上游名称读取错误时，保留该根因 FAIL，下游执行设置/波形准备注明“Blocked by upstream binding”，不再把它们显示成数项独立缺项或暗示 PDK 损坏。独立的网表/文件缺失仍各自保留 FAIL。

## VGS、VDS、VBS 不是三个名字相近的信号

它们分别是 `V(G)-V(S)`、`V(D)-V(S)`、`V(B)-V(S)`。某些 VCO 中的 G、D 命名波形是单端值：只有网表证明源极通过零伏连接等电位于地时，单端值才可作为相应端电压。VBS 命名输出缺失时，脚本保存 B、S 两端并相减，界面标注“Script-derived terminal difference”。

源极不是地时不能继续假设单端值有效，也不能无条件把 VBS 当作 0。新增私有周期副本同时保留源极公共电平 VS，保证四端电压与 VCO 相同。任意 Calculator 脚本不会在识别阶段执行；不受支持的表达式会说明原因。

## PWL 文件从哪里来

正常 ISF 运行无需手工拷贝波形文件：

`当前工况 VCO HB → 同一时间轴的四端波形 → VGS/VDS/VBS 与 VS → 当前工况噪声副本`

每个工况、每个频偏各有独立运行目录。网表相对文件为 `waveforms/vgs.pwl`、`vds.pwl`、`vbs.pwl`、`vs.pwl`，位于该工况的 `replica/netlist/` 下。全局参数默认是 `ISF_VGS_PWL_FILE`、`ISF_VDS_PWL_FILE`、`ISF_VBS_PWL_FILE`、`ISF_VS_PWL_FILE`；可通过对应表绑定真实已声明的其他名字。

`waveforms/manifest.json` 记录端子来源、周期、采样数及逐文件校验值。文件丢失、内容改变、路径越界或不完整时间轴会阻止运行/缓存复用。移动整个私有网表及其 `waveforms/` 后，波形相对引用仍有效。PDK 和 Cadence 的安装路径仍需在新机器重新识别，不能据此声称任何机器都已验证。

绑定时只读检查选定历史、真实端子/偏置源和同一套准备脚本。真实数据的有限性、周期闭合和文件校验在本次 VCO 仿真结束后再次检查。

## Maestro global 与 test 局部变量

可移植 TB 应把 ISF 需要的设计变量放在 Maestro 的 Global Variables 中；不要靠 UI 看不到的 test 局部变量遮蔽全局值。界面显示当前来源及覆盖关系。旧 history 的 test 局部变量暂保留兼容并提示迁移，不会偷偷改原始 Maestro。

Cadence 运行全局联合扫描后，有时会在 test 中留下单点缓存。工具仅在全局组、该 test 的启用依赖和无局部组等结构证据明确时，标记为“Global joint parameter group applies to this test”，同时保留缓存原值供核对；不会仅凭数值等于扫描最小值就猜测它不是覆盖。

同一 Maestro 的两个 test 确需不同值时必须保留差异：例如 MOS 周期参考的栅端 PAC 为 1，普通 DC 测试为 0，不能只搬成同一个 global 值而改变测试含义。i_flicker 的成组扫描、VCO 的 corner 覆盖也必须原样保留并用新 history 验证。

此前名称/变量迁移时，本机 `i_flicker` 原视图是 ADE Explorer；将扫描移到 Global Variables 后，需要该视图以 ADE Assembler 打开。只把变量搬过去而保留 Explorer 模式，可能只运行扫描最小值一个点。当时仅转换其原生视图模式，并验证关闭重开后仍为 25 组成对扫描；没有改电路、扫描值或系统默认设置。这是先前迁移记录，本次 thermal 扩展未再修改原 TB。

全局值对某个 test 的启用开关与局部值不是同一回事：参考 MOS 的全局 `REF_GATE_PAC_V=1` 仅参与周期 test，DC test 使用明确的局部 `0`。界面同时读取 `enabled` 和 `omitted`，显示实际范围及原全局值。迁移后应保存、关闭、重开并检查真实新网表；相同名字重复的原生变量记录也可能让 Spectre 拒绝运行，不能靠允许重复参数或旧成功 history 来跳过验收。

在本机 Cadence 中，vpwlf 的文件属性要使用 `pPar("ISF_VGS_PWL_FILE")` 这类变量表达式；直接填裸名字可能被当成文件名字符串。参考 TB 的独立默认波形应随 cell 保存，全局值按当前库定位解析；UI 的本次运行则在私有网表改为本次生成的相对路径。单独在 Maestro 点运行使用的是参考资产，不自动代表当前 VCO 工况。

## 保留的限制

当前波形执行器使用 HB/HBNOISE，不把 shooting PSS 结果当作 HB。不能安全准备的层级内部偏置源、缺失分析或未解释的表达式会在预检明确阻止。Mdc 仍需要准静态噪声采样，周期副本另用 PWL 测量动态谐波；两者不是互相替代的两种模式。

原生联合扫描可能让多个点共用一个交互式 Spectre 进程和日志。这样的 history 被重命名后，当前严格归属检查可能拒绝，不能把它理解为所有仿真都失败。维护时可仅在本次原生运行进程选择逐点 batch，保留全局联合组与原数值，生成独立网表/日志的新 history；不要通过复制日志、伪改 RDB 时间或删除锁绕过校验，也不需要修改系统级 Cadence 默认设置。

名称对应、路径传递与校验不改变原 flicker 的 ISF 相干求和或生产相位候选算法。新增 thermal 使用独立 PM 投影，不表示已修正旧相位候选的全部限制。物理限制仍见 [参考 TB 说明](REFERENCE_TESTBENCHES.md)。
