## ADDED Requirements

### Requirement: 系统必须能够抓取百度 A 股 JSONP 日 K 数据
系统 MUST 提供针对单只 A 股证券抓取百度 JSONP 日 K 数据的能力。该能力 SHALL 支持根据证券代码构造请求、发起 HTTP 请求并返回原始响应内容，同时记录请求元数据以便后续入库到原始层。

#### Scenario: 根据证券代码发起请求
- **WHEN** 用户提供一只 A 股证券代码并执行抓取命令
- **THEN** 系统 MUST 能构造百度日 K 数据请求并返回对应原始响应内容

#### Scenario: 请求元数据可追溯
- **WHEN** 一次抓取请求完成或失败
- **THEN** 系统 MUST 能记录请求参数、请求时间和请求状态

### Requirement: 系统必须能够解包 JSONP 响应
系统 MUST 能识别并去除百度接口返回中的 JSONP 回调外壳，提取内部 JSON 载荷用于后续解析。若响应内容不是合法的 JSONP 或 JSON 结构，系统 MUST 报告解析错误而不是静默失败。

#### Scenario: 成功解包标准 JSONP 响应
- **WHEN** 输入内容为 `callback({...})` 形式的 JSONP 文本
- **THEN** 系统 MUST 提取内部 JSON 对象文本并进入下一步解析

#### Scenario: 非法 JSONP 响应触发错误
- **WHEN** 输入响应缺少成对括号或内部 JSON 不完整
- **THEN** 系统 MUST 返回明确的解包或解析错误

### Requirement: 系统必须能够解析并标准化日线行情
系统 MUST 解析 `newMarketData.keys` 与 `newMarketData.marketData`，将每条日线记录映射为符合 `daily_bar` 表结构的标准化对象。系统 SHALL 对数值字段执行类型转换，并将 `--` 等占位值映射为空值。

#### Scenario: 按 keys 映射行情字段
- **WHEN** 接收到带有 `keys` 和 `marketData` 的有效响应
- **THEN** 系统 MUST 按字段键名而不是硬编码下标完成日线记录映射

#### Scenario: 缺失值被标准化为空
- **WHEN** 日线数据中的均线或其他指标字段为 `--`
- **THEN** 系统 MUST 将该字段标准化为空值而不是保留原始占位字符串

### Requirement: 系统必须能够解析并标准化事件数据
系统 MUST 解析 `reportData` 中按日期组织的事件数组，并将其展开为符合 `event_record` 表结构的标准化对象。首版事件类型 SHALL 至少支持财报发布类事件。

#### Scenario: 单个日期下多条事件被展开
- **WHEN** 某个事件日期下存在多条事件记录
- **THEN** 系统 MUST 输出多条独立的标准化事件对象

#### Scenario: 财报事件被标记为统一事件类型
- **WHEN** 系统解析样例中的财报发布记录
- **THEN** 标准化结果 MUST 包含可识别的事件类型值用于后续入库和查询

### Requirement: 系统必须写入原始层与标准层数据
系统 MUST 将抓取过程中的原始请求、原始响应、标准化日线行情和标准化事件数据写入现有 DuckDB 表结构。写入过程 SHALL 兼容重复抓取场景，并避免对 `daily_bar` 产生重复主键记录。

#### Scenario: 原始请求与响应被保存
- **WHEN** 一次抓取成功完成
- **THEN** 系统 MUST 在原始层表中保存对应请求和响应记录

#### Scenario: 重复抓取不会生成重复日线主键记录
- **WHEN** 同一证券同一交易日的数据被重复抓取并入库
- **THEN** 系统 MUST 避免生成重复的 `(security_id, trade_date)` 日线记录

### Requirement: 系统必须提供单证券导入入口
系统 MUST 提供一个最小可运行的命令行入口，用于对单只证券执行抓取、解析和入库完整流程。命令执行结果 SHALL 输出导入状态以及写入的记录数量摘要。

#### Scenario: 命令行触发完整链路
- **WHEN** 用户执行单证券导入命令并提供证券代码
- **THEN** 系统 MUST 完成抓取、解析、标准化和入库流程

#### Scenario: 命令行输出导入摘要
- **WHEN** 导入流程结束
- **THEN** 系统 MUST 输出原始响应、日线记录和事件记录的处理摘要
