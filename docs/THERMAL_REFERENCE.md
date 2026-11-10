# 独立 thermal / AM-PM 验收参考

此参考只验证热噪声支路使用的 AM/PM 投影、独立频带功率求和以及电感噪声源单位。它不使用 VCO 的总相噪反推系数，也不改变已验证的 flicker 算法。通过本参考不等于实际 VCO 已签核：实际电路仍受工况、采样密度、谐波截断、准静态假设及数值质量限制。

理论模型、当前自动采集范围、24 相位/振幅 ±32/实际 HB 带宽以及两张**非实测**黑白示意图见 [Thermal 支路说明](THERMAL_NOISE.md)。本页保存的是独立参考电路的仿真验算，不能与那些理论示意图或真实 VCO 结果混称为同一份证据。

## 文件与运行边界

- `tests/fixtures/thermal_reference/analytic_am_pm/input.scs`：三种解析调制路径和对应 HB / HBXF / HBNoise 分析。
- 同目录 `white_source.va.txt`（仓库中以 `.txt` 存放；运行 Spectre 前复制为 `white_source.va`，校验脚本按此对应）：自行编写的已知白噪声电压源，PSD 为 `1e-18 V²/Hz`。
- `tests/fixtures/thermal_reference/inductor_units/input.scs`：固定串联电阻和趋肤效应电感的独立单位参考。
- `scripts/verify_thermal_reference.py`：只读现有 PSF 的验收工具，标准 Python，无 NumPy、Matplotlib 或 GUI 依赖。

这些源文件不含 PDK、厂商模型或用户 TB 数据；仅使用标准 Spectre 元件及 Verilog-A 标准头文件。相对 `ahdl_include` 保持可移植。仓库不附带 Cadence 软件或二进制 PSF。

验收工具**不会启动 Spectre/Ocean，不生成或改写 PSF，不写报告文件，也不修改原始结果**。它只调用用户指定的可信 `psf` 读取器，JSON 输出到标准输出。`--cadence-env` 只在读取器子进程中加载；不把 Cadence 动态库环境注入父 Python，也不修改系统环境。

## 验收现有结果

从项目根目录运行；将下面占位路径替换为要验收的私有独立参考目录，不能与真实 VCO 任务目录混用：

```bash
python3 scripts/verify_thermal_reference.py \
  --psf /path/to/analytic_am_pm/psf \
  --inductor-psf /path/to/inductor_units/psf \
  --psf-tool /path/to/cadence/psf \
  --cadence-env /path/to/cadence_env.sh
```

移植后替换上述路径即可；`--psf-tool` 默认在子进程的 PATH 查找 `psf`。`--inductor-psf` 可省略，但此时 JSON 的 `requested_suites` 只包含 AM/PM，**不能据此宣称电感单位验收通过**。输入目录须保留 `../netlist/input.scs`、需要的 `.va`、`spectre.out` 和相应分析结果。脚本检查输入源文件与仓库固定参考的 SHA-256 一致，避免误读另一套电路。

退出码：`0` 表示所有请求的参考检查通过；`1` 表示数值或来源一致性检查失败；`2` 表示缺失数据、读取器或解析错误。零退出码只代表这套参考，不是实际 VCO 的签核结论。若需要持久报告，调用者可自行保存标准输出；工具本身无写文件选项。

在另一台机器生成新参考属于**单独的仿真操作**：应将对应 fixture 复制到新建的私有 `netlist` 目录，在已配置好的 Cadence 子环境中显式运行 Spectre，并将结果写入同层 `psf`。本脚本不代替该操作，缺少历史数据时也不会偷偷补跑。

## AM/PM 投影与解析值

载波为 `sin(2πf0t)`，`f0=1 MHz`，其峰值复相量约为 `−j V`。检查频偏为 `1 kHz`。对于同一个独立输入频带 `m`，读取：

```text
H⁺[m] = <case>_upper.(m−1).pxf_hbxf 的 VSTIM，输出频率 f0+offset
H⁻[m] = conjugate(<case>_lower.(−m−1).pxf_hbxf 的 VSTIM)，原输出频率 f0−offset
r = conjugate(carrier) / abs(carrier)
H_AM[m] = (r H⁺[m] + conjugate(r) H⁻[m]) / 2
H_PM[m] = (r H⁺[m] − conjugate(r) H⁻[m]) / (2j)
S_AM/PM = Σ_m |H_AM/PM[m]|² × S_white
```

下边带的**索引反转和共轭**不可省略。脚本使用 `m=−7…7` 的共同实测范围；本解析电路非零路径位于 `|m|≤3`。HBXF 传递量单位为 V/V，因此乘入已知电压噪声 PSD 后得到 V²/Hz。这里的 `1/2` 来自 AM/PM 投影，不通过比较 VCO 相噪进行拟合。

| 参考路径 | AM PSD (V²/Hz) | PM PSD (V²/Hz) |
|---|---:|---:|
| `BAM`：白噪声 × sin | 3.75e−19 | 1.25e−19 |
| `BPM`：白噪声 × cos | 1.25e−19 | 3.75e−19 |
| `BPWM`：白噪声 × cos × (1+0.5 sin) | 1.40625e−19 | 3.90625e−19 |

重要：`BAM` 在 **m=0 的低频输入**下是纯 AM，投影 PM 为零；但这里注入的是宽带白噪声，`m=±2` 仍可折叠形成 PM。因此不能要求它的宽带总 PM 为零，也不能用这一非零值判定投影错误。`BPM` 同理并非“任何输入频率下都只产生 PM”。

每项检查有三条独立比较：HBXF 投影功率和对解析值、HBNoise 输出对解析值，以及二者互相比较。另检查 `W.known_white` 器件贡献与总输出一致。HBNoise AM/PM 的 `out` 类型为 `V/sqrt(Hz)`，要**平方**成 PSD；`W.known_white` 的结构字段已是 `V²/Hz`，不能再次平方或开方。

Spectre 18.1 系列的六项比较均通过；传递量功率和对 HBNoise 的相对误差处于浮点舍入量级。默认相对容差 `1e−9`，不是通过数据调整出的拟合容差。

## 电感串联热噪声单位参考

固定电感为 `L=200 pH, R=10 Ω`，温度 `27 °C`，负载 `50 Ω` 且关闭负载自身噪声。趋肤效应电感使用 `mode=2, q=20, fq=5 GHz, rdc=0.01 Ω`。两者分别验证 `10 kHz、1 MHz、5 GHz、10 GHz`。

该版 Spectre 的 `inductor` 结构字段把 `rn.src` 标为 `A²/Hz`，但固定 R 的实际数值 `1.65757549356e−19` 对应 **4kTR 的串联电压噪声 PSD**，不是 4kT/R。与现代精确 SI Boltzmann 常数的差约 `1.9e−5`，符合旧版常数差异；脚本保留该差值，使用 `3e−5` 容差，不用参考值校准常数。

独立 AC 激励为 `vin=1 V`，故 `out_fixed/vin` 或 `out_skin/vin` 是已知串联电压注入位置的 V/V 传递量。验证：

```text
S_out = |Vout/Vin|² × rn.src
```

预测值必须同时匹配原噪声分析的 `rn.out`（结构内 PSD），且 AC 增益平方匹配 `rn.xf2`。趋肤效应的 `rn.src` 随频率变化，逐频点使用，不能拿单个频点代替全频谱。独立 AC 复数输出被 `psf` 工具打印为约六位有效数字，因此此比较使用 `1e−5` 容差；结构内 `src×xf2` 对 `out` 保持 `1e−9` 容差。

本机八个频点均通过。此结论只授权该已验证原生电感 `rn` 的**串联电压端口**，不授权把任意标成 A²/Hz 的字段当成电压噪声。解析器始终保留原始单位。

另一个已知歧义：本静态 noise 结果的顶层 `out` 标签写 V²/Hz，数值却表现为 ASD。验收工具**不使用这个顶层字段**，只使用明确的器件结构 `rn.out`。这不能推广到 HBNoise；上述 HBNoise `out` 的 ASD 标签是正确的。

## JSON 证据与适用边界

JSON 包括参考版本、读取到的 Spectre 版本、源文件与每个已使用 PSF 的 SHA-256、运行完成日志、各独立输入频带的传递量与 PSD、预期值、误差、容差和通过状态。源文件不同或混入不同 Spectre 版本时不能通过整体验收。

这套参考没有 MOS、PDK 或真实振荡器。它验证的是投影/单位/求和约定，不证明准静态 MOS 噪声模型、相关栅漏噪声、实际 VCO 截断阶数或双器件对称假设成立。原始 PSF、已声明质量警告与实际 VCO 的独立仿真对比仍须保留；不能只保留一个 `pass=true`。

生产支路当前仅对已验证的 Spectre 18.1 / BSIM3v3 gm 器件对、支持的原生电阻和电感机制做有限范围自动处理，**不等于支持所有无源网络**。本参考中的已知 Verilog-A 白噪声源只用于独立验收约定；生产采集尚未为自定义 Verilog-A `thermal_noise` 建立端口支持。参考的八阶 HB 及六项解析验算不会赋予其他器件模型同样的适用性；无法建立有效 thermal 证据时显示 N/A，而不是当作零噪声。数值质量告警与模型范围须与参考通过状态分别报告。
