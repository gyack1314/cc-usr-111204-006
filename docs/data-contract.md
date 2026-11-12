# 寄存器服务约定

地址均为零基：配置里的 `address` 直接作为接口地址，文档中的 40001 之类编号不发送。ReadHolding 对应功能 03，ReadInput 对应功能 04；适配器只发出这两种读请求，任何运行记录中都不应出现写功能（05/06/15/16/22/23），虚拟源在收到非读操作时立即拒绝且不消耗响应步骤。

`words` 为 0 到 65535 整数列表。适配器返回异常时用 `exception` 字段且不携带 `words`；exception 字节不解成正常值。`allowed_ranges` 是含端点的零基地址段，每个批读必须整体落在单个地址段内，绝不跨越禁读空洞；批读字数不超过 `max_words`。

## 字段声明

每个字段声明 `area`、`address`、`words`、`type`（uint16/int16/uint32/int32/float32/bits）、`byte_order`、`word_order`，以及精确有理数 `scale`/`offset` 字符串。位段另声明 `lsb`、`width`、`signed`，固定占一个字：

- `byte_order` 作用于每个 16 位字的字节顺序；
- `word_order` 作用于多字字段的字顺序；
- 先按两种顺序形成规范的大端字节，再按类型解释；
- 位段先在**无符号原字**上按 `lsb`/`width` 提取，再按声明宽度解释符号位。

重叠字段可以共享同一次读取的原始字，但它们必须同区、同 unit、同地址段；跨区或跨 unit 的字段不能拼成一次读取。字段本身不得跨允许地址段（即不得跨禁读洞），计划也不会把字段切断到两个批次里。

`scale`/`offset` 只作用于最终工程量，用 `fractions.Fraction` 精确计算；JSON 中原值以整数或分数文本（如 `"65575/4"`）保存，不经过二进制浮点。

## IEEE 与厂商哨兵

`float32` 默认 `ieee_special: "error"`：出现 NaN/±Inf 时整组拒绝，绝不伪装成 0。可选策略：

| 策略 | 行为 |
| --- | --- |
| `error` | 整组解码失败，不发布样本 |
| `preserve` | `value`/`engineering` 为 null，`preserved.ieee` 与 `preserved.bits` 保留位模式 |
| `null` | `null: true`，`state: "ieee_null"`，保留位模式 |
| `sentinel` | 声明 `sentinel`（十进制字符串，必须可精确表示为有限 float32）；位模式命中时 `presence: "read_vendor_sentinel"`，区别于缺读 |

缺读（异常、字数不符、世代变化）不会出现在字段里，而是让**整组**无效；字段证据与特殊值只在整组被接受后发布。

## 世代与接受判据

`generation` 为设备独立维护的 16 位计数（可声明 `bits` 小于 16，按声明宽度取值）；奇数表示更新中、偶数表示稳定。一次正式采样先读 generation，再按计划读全部数据块，最后再读 generation：

- 起始或结束世代为奇数：本次尝试无效；起始奇数时不继续读取数据块；
- 两次均为偶数但数值不同：更新发生在采样期间，整组无效（不拼接）；
- 两次相同且为偶数：整组来自同一更新周期，**只有此时**发布样本；
- 任何数据块异常、返回字数不符、解码失败：整组无效；
- 重试整组（generation + 全部块 + generation），而不是只补失败字段；
- 预算耗尽时输出原尝试与未接受原因，不发布“部分正常”的正式样本。

`max_sample_duration_ms` 必须严格短于 `counter_wrap_min_ms`（设备保证的完整回绕最小时长）。这样“两次相等且偶数”才足以排除计数器在采样期间完整回绕后重现同一数值；否则配置在解析阶段被拒绝。不对任意设备计数器自动推导该保证。

## 采样结果

接受后的样本包含 `map`（`device`/`unit`/`map_revision`/`areas` 身份与 `map_sha256` 映射摘要）、`generation`（起止原字与计数值）、`request_order`（每次读请求及其响应的原字）、逐字段 `words`、`raw_bits`、`raw_hex`、位段提取值、`value` 与 `engineering`。它只证明本 unit 这组值来自同一更新周期，**不是**跨设备同时快照；该限制同时写在 `claim` 与 `not_a_snapshot` 字段中。失败时 `accepted`/`published` 为 false，`attempts` 保留每次尝试的原因与已发出的请求顺序。

`domain` 只数映射字数，不规划、轮询或解码。
