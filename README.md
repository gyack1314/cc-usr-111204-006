# 电解槽寄存器映射的同代只读采样

此 Python 项目演示如何在没有设备大快照、只能分段读取寄存器的前提下，得到**同一更新周期**的整组读数：先读世代计数，再按计划读全部数据块，最后再读世代；两次相同且为偶数才接受整组。它解决的是“电流来自新周期、压力仍是旧周期”的拼接误报。

`data/` 为原始样本及访问配置，`source_data.io` 负责严格读取和字节身份，`source_data.mapping` 校验映射并生成不跨禁读洞的读取计划，`source_data.codec` 做纯解码，`source_data.sampling` 执行只读轮询与接受判据，`source_data.replay` 按声明顺序返回不解释的虚拟源响应。字段、解码和世代判据的完整口径见[资料约定](docs/data-contract.md)。所有机构、设备、对象与数值均为虚构；资料为合成字表及解码响应，不连接或控制真实设备或网络。

Python 3.11 及以上即可使用，无第三方 Python 依赖。项目根目录运行：

```sh
python -m unittest discover -s tests -v
python -m compileall -q source_data tests
python -m source_data.cli inspect data/normal.json
python -m source_data.cli sample data/normal.json
python -m source_data.cli sample data/torn-generation.json
```

`inspect` 展示已有的原始库存，不修改资料。`sample` 只回放资料里 `steps` 声明的虚拟源响应，不起网络连接、不发写请求。`invalid-` 前缀文件为不合法输入；其他变体保留不同业务边界。源协议与二进制格式使用资料约定中的有限范围，未解释部分仍以原字节保存。

虚拟源覆盖以下复演：四种字节/字顺序组合、跨禁读空洞的批读约束、更新恰好发生在两个数据块之间（`torn-generation.json`）、异常码（保留 `exception` 并区别于解码失败）、IEEE NaN/Inf 与厂商哨兵策略。任何采样的 `request_order` 只会出现 `ReadHolding`/`ReadInput`，每一份运行记录都不会出现写功能。

固定基础镜像见 Dockerfile，可执行 `docker build -t energy-source .`，再用 `docker run --rm energy-source` 显示摘要；`docker run --rm energy-source python -m unittest discover -s tests -v` 执行检查。资料和回放不依赖真实设备、账户或实时网络服务。
