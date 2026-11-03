# 项目审阅与改进方案

审阅日期：2026-11-03

范围：`Geo_data_v3.0` 数据采集与生成项目，以及同级 `Geodataing` 前端、Vercel API 和服务端查询代码。

用户确认补充：采集阶段失败后，继续使用数据库已有数据生成和更新，网站无需区分显示未更新部分。已从 GitHub 主分支确认现有归档日志工作流，保持每两周重新抓取的安排。以下建议按这两项现有行为修订，不因采集不完整阻止正常生成，也不新增网站提示。

## 再次复核与首批实施记录

用户要求重新核对后，已先用离线脚本复现，再修改正式代码。本文后面的“问题”和原始验证结果记录的是审阅时的基线，不表示这些问题在修改后仍全部存在；当前进度以本节为准。

再次复核保留的修正：归档 Action 会隔周重查，limit/断点问题只涉及复用状态文件；健康检查只对 logs 做全表计数，另一个无 FROM 的 COUNT 实际为 1；头像已有六个并发 worker，问题是预加载范围过大；216 次查询是旧实现的高分布样例，不是每次线上请求的测量值。系列详情加载时已有占位保护，没有将其误列为“显示旧系列数据”的缺陷。

| 报告项 | 当前进度 | 实际改动或边界 |
| --- | --- | --- |
| 1 普通报表发布 | 已修复主要失败路径 | 五份报表完整生成、拒绝非有限数值、校验共同时间戳后才替换；普通替换异常回滚，回滚失败保留恢复文件；CLI 明确失败 |
| 2、3 排名口径与失败零分 | 已修复并做离线交叉比较 | 按生成器过滤规则计算区域/全局分数与并列名次；查询失败抛错；活动、新人只查实际使用的 all 范围 |
| 4 错误响应缓存 | 已修复 | HTTP 成功及数据结构、用户身份校验后才缓存；旧错误对象自动丢弃；存储不可用时仍可继续请求 |
| 5 owner GUID | 已修复 | 未转移保留身份；转移后用新 GUID，无法核实时不沿用旧身份 |
| 6 归档续跑 | 已修复 | limit 在已完成项和账号组过滤之后应用；失败保存断点后非零退出；隔周 Action 不变 |
| 7 注册资料分字段状态 | 待用户决定 | 已解释新增字段的原因；未改用户表结构或自动补抓规则 |
| 8、9 API 与健康检查 | 已实施基础修复 | 玩家参数及 GET 边界、通用错误响应、轻量 SELECT 1；提交接口保留原有校验和成功结构 |
| 10 查询开销 | 已修复请求扇出及缓存 | 冷请求为 24 条统计 SQL，加 BEGIN/COMMIT 共 26 次往返；单个只读一致性事务；5 分钟、最多 128 用户缓存及在途合并。用户明确暂时跳过查询超时限制，保留现有连接池和超时行为 |
| 11 预加载与拆包 | 已修复主要问题 | 取消全站榜单/头像预取，只预取当前榜单显示的头像；各页面按需加载，导航仍保留 |
| 12 失败恢复和可访问性 | 已完成一批 | 榜单请求失败可重新调用；PK 标题、标签、焦点、Tab/Escape；D/T 按钮支持焦点和点击；未做完整浏览器视觉验收 |
| 13 可复用测试 | 已落地 | 两端新增正式 tests、离线命令和只读 CI；旧维护目录不参与自动发现；共享契约进一步版本化尚未完成 |
| 14 单一服务端源码 | 待用户选择 | 已解释 TS 编译生成 JS 与直接维护 JS 的影响；尚未切换，也没有改部署构建规则 |
| 15 只读生成与迁移分离 | 后续阶段 | 此次只处理文件发布保护，头像补抓/建表分离及全报表数据库快照尚未实施 |
| 16 环境与资源治理 | 部分落地，其余待核查 | 采集失败后照常生成已落实；未修改数据库 TLS、线上限流、索引、保留期限、依赖版本或 SSH 配置 |

文件发布保护不等同于进程崩溃时多个固定 URL 的严格原子切换；当前保证生成/校验失败不触碰旧报表，普通替换异常尝试回滚。仍保留原文件 URL 和字段形状，所有普通报表共享一次生成时间，没有新增网页提示或字段。

前后端口径测试直接取两边的实际 SQL，用内存 SQLite 的少量 PostgreSQL 语法适配对同一批样例比较 24 种类型/时间组合、多个玩家及区域结果；它验证统计语义，不能替代真实 PostgreSQL 执行计划与线上压测。

验证更新：本轮后端完整检查时，228 项测试中 227 通过、1 项缺少 PyYAML 跳过；前端最终离线测试 73 项全部通过，类型检查通过。原本失败的本地审核 HTTP 测试在允许环回访问后通过，审核业务代码未因此修改。使用正常 Vite 配置、输出到独立目录的生产构建通过；原 dist 清理曾因 Windows 文件访问限制失败，未强行删除。

并行工作边界：收尾时用户确认另一任务正在修改 Certitude 的 403/429 冷却重试。`crawl_certitude.py` 及该任务新增/修改的测试和临时文件由另一任务负责，本轮不覆盖、不清理，也不将其归为本轮实现。上述后端全套测试计数为另一任务这些修改出现前的检查结果。

构建主入口 JS 从审阅基线约 459.6 kB 降至约 298.7 kB，页面文件另按需加载；这不是首屏总传输量或线上加载速度的实测。此次未连接正式数据库、运行真实采集或发送通知，也未执行 Git 备份、提交、推送或部署。

## 原始审阅结论

现有“定时采集 → PostgreSQL → 静态报表，个人查询和社区提交使用 API”的总体方向适合当前产品。建议保留这条主线，先修复数据正确性和失败处理，再优化请求量及维护方式，无需先整体重写。

最值得优先处理的是：普通报表失败仍返回成功、静态榜单与“我的排名”统计口径不一致、失败响应进入浏览器缓存、藏点更新可能丢失 owner GUID。这些问题会造成用户看到错误或不完整的数据。

Certitude 的纯计算模块、批次校验、同一只读事务和原子入口切换值得推广到其他报表；审核接口已有会话验证、同源验证、事务与行锁，也应保留。

## 原始审阅方式与边界

- 阅读两边的 README、交接文档、共享数据契约、采集/生成主流程、工作流、API、服务端查询、前端路由与主要页面、工具和测试目录。
- 对影响决策的代码路径做交叉核对；大型分析脚本和实验工具以职责、数据库访问方式和现有测试为主进行抽查。
- 没有访问真实数据库、启动采集器、调用外部站点、发送邮件或触发发布；没有读取或输出环境文件中的密钥值。
- 没有更改业务代码、数据库、Git 配置，没有备份、提交或推送。
- 不能仅从仓库判断生产数据库的索引、规模、权限、Vercel Firewall 规则或线上访问延迟。本报告区分确认的问题与需要运行环境验证的风险。
- 未做浏览器交互、移动端视觉验收或真实数据库执行计划分析；页面体验结论以代码和本地资源为依据，性能数字不冒充线上测量。
- 补充确认时只读获取了 GitHub 主分支的归档工作流文件，确认线上仓库已包含隔周调度；没有获取最近运行记录，因此不宣称最近某次采集已成功。

## 原始验证结果

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| Python Certitude 测试 | 207 项中 206 通过，1 跳过 | `python -B -m unittest discover -s tests -p 'test_certitude*.py'`；跳过项为未安装 PyYAML 时的 YAML 解析检查 |
| 前端类型检查 | 通过 | `npm run lint`；当前脚本实际执行 `tsc --noEmit` |
| 前端 Certitude 离线测试 | 38/38 通过 | static、flow、insights 三组 |
| 审核相关测试 | 17 通过，1 失败 | 本地 HTTP 测试访问模拟服务器时 `fetch failed`，未完成集成验证，不能据此认定业务有错或测试全通过 |
| 隔离生产构建 | 通过 | 使用当前安装的 Vite、React、Tailwind 插件；未加载环境文件、未复制 public 数据、未运行原配置中的本地 API 插件；不等同于完整部署验证 |
| 普通报表失败注入 | 问题复现 | 一个报表失败后，旧报表与新文件、新时间戳并存；`main()` 正常返回 |
| 个人 API 模拟测试 | 问题复现 | 数组 username 在异常处理之前抛错；500 响应包含原始异常文本 |
| 全排名接口模拟测试 | 问题复现 | 查询失败返回零分；全维度有分值时发出 216 次查询；发现 SQL 口径和别名问题 |
| Layout 预取模拟测试 | 问题复现 | HTTP 500 的 JSON 被写入两个正常数据缓存键 |
| 采集代码检查 | 证据确认 | 核对 GUID 覆盖、归档 limit/断点顺序与失败退出逻辑；未运行真实采集 |

用于此次审阅的一次性验证脚本均建在后端 `test/` 下，执行后已删除；隔离构建产物已删除。测试结论是当前本地代码的结果，不代表线上状态。

## 第一优先级：数据正确性与失败处理

### 1. 普通报表生成失败仍可能发布缺失或混合批次的数据

证据：[generate_data.py:3320](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/generate_data.py:3320)、[generate_data.py:3354](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/generate_data.py:3354)、[crawl.yml:358](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/.github/workflows/crawl.yml:358)。

`generate_all()` 逐个打开正式文件写入，捕获异常后继续；`main()` 只记录失败列表，没有抛错或返回失败退出码。工作流随后复制当前存在的 JSON。新 Actions runner 上可能缺少失败文件，目标仓库又保留上次版本；本地也可能直接留下部分写入的文件。与此同时 `generated-at.json` 仍可成功更新。

改进：所有报表先写入独立临时批次，验证文件完整、JSON 合法和关键统计关系后再切换入口；任何必需文件失败都返回非零状态。为普通报表增加统一 `datasetVersion` 和数据基准时间。可以复用 Certitude 的设计原则，但不必立即改变所有公共 URL。

这里的批次完整性针对生成文件自身，不要求源数据必须刚刚采集成功。按用户确认的策略，采集失败时使用已有数据库内容正常生成，网站无需区分显示。

验收：注入任意一个生成/序列化失败，已发布完整批次及时间戳保持不变，任务显示失败；成功批次所有文件版本一致。

### 2. “我的排名”与静态榜单使用不同的统计条件

证据：前端 [player-all-ranks.js:36](C:/Users/cjyte/Documents/trae_projects/Geodataing/server/player-all-ranks.js:36)、[player-all-ranks.js:144](C:/Users/cjyte/Documents/trae_projects/Geodataing/server/player-all-ranks.js:144)，对照 [generate_data.py:1042](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/generate_data.py:1042)。前端 [PlayerRankings.tsx:166](C:/Users/cjyte/Documents/trae_projects/Geodataing/src/pages/PlayerRankings.tsx:166) 会在榜单中没有当前用户时使用这些结果。

- `finds` 的动态查询没有限制成功找到/参加活动的日志类型，会把 DNF 等日志涉及的藏点计入。
- 动态 `favorites` 汇总藏点当前 `favorite_points`，时间范围按藏点发布时间；静态榜单按日志访问日期和 `favorite_point_used` 统计。
- 动态查询缺少静态榜单所用的部分类型、删除状态及归档范围过滤。
- `events_hosted` 的 30d/ytd 得分查询仅有 `FROM caches c`，却拼入 `l.visited`，引用不存在的别名。当前页面活动排名使用 all 范围，因此这一别名问题不直接影响当前活动页展示，但请求仍会执行这些无效分支。

改进：把“我的排名”视为同一榜单的任意用户查询，逐项对齐契约，避免再维护一套独立 SQL 规则；活动和新人只查询产品实际支持的维度。

验收：同一数据库快照下，静态榜单与动态用户查询的分数和并列名次完全一致。固定样本应包含 DNF、活动、归档、404、跨年 FP、并列、大小写用户名和零分用户。

### 3. 全排名接口把数据库失败转换为真实零分

证据：[player-all-ranks.js:196](C:/Users/cjyte/Documents/trae_projects/Geodataing/server/player-all-ranks.js:196)、[player-all-ranks.js:235](C:/Users/cjyte/Documents/trae_projects/Geodataing/server/player-all-ranks.js:235)。

查询 `.catch()` 直接生成 0 分或 null 排名，然后正常返回响应。模拟测试中所有查询都失败，接口仍产出零分对象。临时连接故障、SQL 错误与真实无记录因此无法区分。

改进：返回明确的失败状态；若允许部分结果，附带完整性信息，未完成项使用未知状态。不要把失败对象存为正常成绩。

验收：连接失败与真实零分在接口状态、页面文案和缓存行为上均可区分；服务端保留可追踪的错误记录。

### 4. 网站预取会把错误响应缓存到次日

证据：[Layout.tsx:226](C:/Users/cjyte/Documents/trae_projects/Geodataing/src/components/Layout.tsx:226)、[dailyCache.ts:3](C:/Users/cjyte/Documents/trae_projects/Geodataing/src/utils/dailyCache.ts:3)、[PersonalStats.tsx:234](C:/Users/cjyte/Documents/trae_projects/Geodataing/src/pages/PersonalStats.tsx:234)。

Layout 两次预取都直接 `r.json()` 后存储，没有检查 HTTP 状态或数据结构。缓存截止到浏览器本地下一次 06:00。个人统计页面读到任何真值对象就作为有效结果并直接返回；一次 500 因此可能在服务恢复后仍阻止正常查询。

改进：只缓存 HTTP 成功且结构有效的数据；错误使用独立短期状态；读取旧缓存也校验并自动丢弃无效对象。提供用户可见的重新加载入口，并明确缓存时间与数据生成时间的区别。

验收：预取返回 500、无效 JSON、缺少字段时不进入成功缓存，下一次访问可自动恢复。

### 5. 藏点普通更新可能清空已有 owner GUID

证据：[crawl_caches.py:600](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/crawl_caches.py:600)、[crawl_caches.py:1047](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/crawl_caches.py:1047)、[crawl_caches.py:309](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/crawl_caches.py:309)。

地图结果没有 owner GUID，解析结果为 None；其他元数据变化时，新记录被整体写回，而 upsert 直接采用 `EXCLUDED.owner_guid`。用户名未变化时没有恢复原 GUID 的分支，已补齐的身份信息可能在 FP、名称等字段更新时丢失。

改进：owner 未变化时保留已有 GUID；确有转移时将“新身份待确认”与“沿用旧 GUID”明确区分，不能简单对所有情况统一 COALESCE。

验收：FP/名称/状态变化保留 GUID；owner 转移且详情获取失败时，不把旧 owner GUID 误认为新 owner 的身份。

### 6. 归档采集失败仍正常退出，limit 与续跑也会相互干扰

证据：[crawl_archived_logs.py:391](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/crawl_archived_logs.py:391)、[crawl_archived_logs.py:412](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/crawl_archived_logs.py:412)、[crawl_archived_logs.py:711](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/crawl_archived_logs.py:711)。

- SQL 先 `LIMIT N`，之后才跳过本地断点已完成项。重复使用状态文件时，前 N 项全部完成就可能选不到后续项目，导致续跑停滞。
- 普通单项失败被计数、记录后，主函数仍正常退出，工作流不能仅凭退出状态识别不完整运行。
- 独立 Actions 目前只上传归档状态文件，不在下次运行恢复；所以“长期跳过已完成项”适用于复用本地状态的运行，不能直接套用于每次新 runner 的定时任务。

补充核验：GitHub 主分支的 [Archived Logs Crawler 工作流](https://github.com/EPoNeNT/Geo_data_CN_online/blob/master/.github/workflows/crawl-archived-logs.yml) 已包含 `0 20 * * 0` 每周触发，以及以 2026-05-24 20:00 UTC 为锚点的隔周执行判断，即北京时间隔周一 04:00 实际抓取。定时调用不传 `--limit` 或 `--skip-db-crawled`，新 runner 也不恢复抓取断点；数据库候选为全部 `cache_status = 2` 的藏点，普通和 Premium 两组均处理。因此现有自动任务会定期重新核对归档日志，无需再新增一个同用途 Action。该结论确认的是配置和选取逻辑，不保证每次都在 360 分钟内全部抓取成功。

改进：保持现有隔周定时重查；针对复用本地断点的手动小批运行，先确定待处理集合再取 limit，或使用明确的扫描游标；普通失败达到本次不完整条件时返回非零状态，保留可续跑断点。失败状态供运行维护使用，不阻止其他流程从已有数据库生成，也不要求网站显示差异。

验收：连续两次 `--limit 2` 能推进到后续藏点；有待重试项时工作流可识别；新 runner 与本地续跑行为各有测试。

### 7. 用户注册资料与首个找到地区共用一个成功状态

证据：[crawl_user_regdates.py:486](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/crawl_user_regdates.py:486)、[crawl_user_regdates.py:710](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/crawl_user_regdates.py:710)、[crawl_user_regdates.py:640](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/crawl_user_regdates.py:640)。

注册日期成功、首个找到国家请求失败时，仍可能保存 `fetch_status='ok'` 和空 `reg_place`；后续候选筛选又排除 ok 用户，常规任务不会再尝试这一字段。独立补填工具存在，但不能替代正常重试状态。

改进：注册日期与地域字段分别记录状态、失败原因和下次重试时间；私密、确实无记录、暂时失败应分开。不要把 `reg_place` 解释为居住地或国籍。

验收：注册日期成功、地域请求失败的用户在后续任务中仍会按策略重试，已确认私密者不会无限高频请求。

## 第二优先级：接口边界、性能和用户体验

### 8. 公共 API 参数和错误返回需要统一

证据：[personal.js:13](C:/Users/cjyte/Documents/trae_projects/Geodataing/api/personal.js:13)、[personal.js:42](C:/Users/cjyte/Documents/trae_projects/Geodataing/api/personal.js:42)、[personal-pk.js:3](C:/Users/cjyte/Documents/trae_projects/Geodataing/api/personal-pk.js:3)、[series.js:16](C:/Users/cjyte/Documents/trae_projects/Geodataing/api/series.js:16)。

个人 API 对未校验的 username 直接 `.trim()`，数组参数会在主 try/catch 前抛错；部分读取接口未限制请求方法。多个 API 将底层异常文本返回给客户端，可能暴露查询或内部结构信息。

改进：抽取公共方法、参数类型、长度、重复参数和响应处理；错误细节留在服务端，只给用户可理解的信息及请求编号。保留现有参数化 SQL。

验收：数组/空值/超长用户名/错误方法得到稳定的 400 或 405；模拟数据库错误不会出现在响应正文。

### 9. 健康检查本身有错误统计，并执行昂贵查询

证据：[health.js:5](C:/Users/cjyte/Documents/trae_projects/Geodataing/api/health.js:5)。

SQL 的外层 `COUNT(*)` 没有 `FROM caches`，所以 `cache_count` 实际为 1；日志数量子查询则统计整张 logs 表。接口也直接返回数据库错误文本。

改进：健康检查只做轻量连接验证；如果用户需要总量，从已生成报表或单独受限的诊断接口读取。不要通过给健康检查再补一个全表 COUNT 来解决性能问题。

验收：检查只执行有限且可超时的轻量查询；总量有独立、准确的数据来源；错误信息经过处理。

### 10. “我的排名”请求扇出过大，数据库连接缺少统一限制

证据：[player-all-ranks.js:178](C:/Users/cjyte/Documents/trae_projects/Geodataing/server/player-all-ranks.js:178)、[player-all-ranks.js:181](C:/Users/cjyte/Documents/trae_projects/Geodataing/server/player-all-ranks.js:181)、[data-service.js:34](C:/Users/cjyte/Documents/trae_projects/Geodataing/server/data-service.js:34)。

9 类 × 4 范围先计算分数，再按 5 个区域分别计算排名。模拟所有区域都有分数时共 216 次查询；这不是线上每次请求的实测值，但说明结构上的查询上限。Layout 会为已选择用户名的用户预取结果。普通数据连接池未像审核池一样显式配置连接数和查询超时。

改进：合并分组聚合、共享排名结果或每日预计算用户索引；只请求当前功能需要的维度；设置连接数、连接超时和语句超时；对相同请求合并在途计算。已有缓存应保留并补充容量及失效管理。

验收：相同功能的数据库往返数显著下降；以真实数据记录冷/热请求的查询数、耗时分位数与连接峰值，再决定索引。仓库已有性能索引脚本，不应在未核对现有索引与执行计划时重复创建。

### 11. 全站榜单和头像预加载范围过大

证据：[App.tsx:21](C:/Users/cjyte/Documents/trae_projects/Geodataing/src/App.tsx:21)、[playerAvatarPreload.ts:23](C:/Users/cjyte/Documents/trae_projects/Geodataing/src/utils/playerAvatarPreload.ts:23)、[AvatarImg.tsx:157](C:/Users/cjyte/Documents/trae_projects/Geodataing/src/components/AvatarImg.tsx:157)。

进入任意页面约一秒后都会加载玩家榜单并预处理头像。本地玩家 JSON 为 1,767,340 字节，包含 754 个不同头像 URL。预加载会过滤默认头像，且已有六个并发 worker，不能理解为同时发送 754 个请求；但后台仍会持续处理大批当前不可见的头像。

改进：优先当前页面可见玩家和当前用户；后续小批空闲预取，页面切换后允许停止；保留去重和并发控制。头像处理缓存也应考虑尺寸键及容量。主路由目前除 Certitude 外大多直接导入，可继续按页面拆包。

验收：打开总览不再自动处理整份玩家头像；首屏列表头像及时出现，滚动和切换不明显闪烁。

本地四份主要报表原始体积分别约为：Cache 排名 5.81 MB、总览 1.88 MB、玩家 1.77 MB、城市 1.61 MB；这些是磁盘 JSON 体积，不是压缩后的网络传输量。隔离构建的主 JS 为约 459.6 kB，gzip 约 127.4 kB。优先减少无关资源请求，再依据网络与解析测量决定拆分数据。

### 12. 加载失败恢复和无障碍需要补齐

证据：[playerAvatarPreload.ts:10](C:/Users/cjyte/Documents/trae_projects/Geodataing/src/utils/playerAvatarPreload.ts:10)、[Overview.tsx:1226](C:/Users/cjyte/Documents/trae_projects/Geodataing/src/pages/Overview.tsx:1226)、[PkDialog.tsx:168](C:/Users/cjyte/Documents/trae_projects/Geodataing/src/components/PkDialog.tsx:168)。

- 玩家榜单加载器长期缓存 Promise，首次请求拒绝后未清空，当前会话内再次调用仍得到同一个失败对象。
- 总览 D/T 详情主要依赖鼠标悬停，普通 div 没有对应键盘操作。
- PK 对话框没有完整的可访问名称、输入标签和焦点进出管理；一些页面只有无说明的加载旋转图形。

改进：请求失败时清空在途 Promise，提供无需整页刷新的重试；图表格子支持焦点、键盘和触屏点击；对话框补标题关联、输入标签、焦点约束和关闭后的焦点恢复；加载状态提供可读文字。

验收：只用键盘可查询、查看 D/T 详情、打开和关闭 PK；读屏能识别弹窗与加载状态；一次短暂网络故障后可恢复。

## 第三优先级：降低后续维护与部署风险

### 13. 让自动测试与权威契约进入版本管理

证据：[后端 .gitignore](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/.gitignore)、[前端 .gitignore](C:/Users/cjyte/Documents/trae_projects/Geodataing/.gitignore)、[package.json](C:/Users/cjyte/Documents/trae_projects/Geodataing/package.json)。

后端 `test/` 和前端 `test/` 整体被忽略，里面既有可复用回归测试，也有真实数据库诊断和修改工具。后端已提交的 `tests/` 主要覆盖 Certitude；核心采集和普通生成的许多验证留在被忽略目录。前端没有统一 test 脚本，当前 lint 只是类型检查。后端权威数据契约也被忽略，跨仓库协作依赖本地文件。

改进：将纯离线测试移入已跟踪的 `tests/`；诊断/维护工具另设明确入口，避免自动发现时误连数据库；临时探针用完删除。契约以可提交的共享 schema、固定样本与字段说明形成单一来源，增加跨仓库一致性检查。

验收：全新克隆不依赖本地忽略文件即可运行离线测试；默认测试绝不访问生产；普通报表、采集身份保留、API 参数、缓存失败恢复都进入 CI。

### 14. 消除手工维护的双份运行逻辑，逐步拆分大模块

证据：[data-service.ts](C:/Users/cjyte/Documents/trae_projects/Geodataing/server/data-service.ts)、[data-service.js](C:/Users/cjyte/Documents/trae_projects/Geodataing/server/data-service.js)、[shared/data.ts](C:/Users/cjyte/Documents/trae_projects/Geodataing/shared/data.ts)、[shared/data.js](C:/Users/cjyte/Documents/trae_projects/Geodataing/shared/data.js)、[AI_AGENT_CONTEXT.md](C:/Users/cjyte/Documents/trae_projects/Geodataing/AI_AGENT_CONTEXT.md)。

API 实际导入 JS，但交接文档要求人工同步 TS/JS；另外还有独立全排名逻辑、正式/测试版 generator。重复规则已经出现实际口径漂移。

改进：选定单一源码，让运行 JS 从构建产生，或统一运行语言；先锁定行为样本再拆模块。后端按查询/纯聚合/身份补抓/校验/发布分层；前端把大型 PersonalStats、SeriesCaches、Overview 拆成明确功能组件及数据 hook。不要同时做结构大改和统计规则变更。

验收：开发者修改一次即可影响真实运行入口；构建检查阻止双份源码漂移；拆分前后输出用固定样本比较一致。

### 15. 数据生成应与资料补抓、建表迁移分离

证据：[generate_data.py:282](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/generate_data.py:282)、[generate_data.py:501](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/generate_data.py:501)、[generate_certitude.py:205](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/generate_certitude.py:205)。

普通生成流程会联网补头像、写头像缓存，并在运行时执行建表及 ALTER（包括删除旧列）。它不保证所有报表在同一个数据库快照中读取；当其他采集任务同时修改数据库时，不同报表可能看到不同状态。Certitude 已使用单个 REPEATABLE READ 只读事务。

改进：资料补抓先独立完成，数据库迁移有明确版本和独立执行入口，生成阶段只读并固定快照、时区和基准日期。头像缺失时先输出默认头像，不必阻止统计批次。

验收：断网仍能从现有数据库生成完整数据；只读权限可运行生成器；同批报表的总量、子集和时间范围可校验。

### 16. 将运行状态、资源保留和平台配置变成可核查事项

- 工作流多个后续 job 使用 `always()`，上游失败后仍可能继续生成：[crawl.yml:331](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/.github/workflows/crawl.yml:331)。这符合用户已确认的“使用已有数据更新”策略，应保留；阶段成功/失败记录仅用于运行维护，网站无需区分显示。需要进一步对齐的具体条件是：Certitude 生成步骤当前在其采集 job 失败或取消时会跳过，可在实施时改为只依赖本次生成阶段可正常执行，从已保存数据生成；注册资料补抓步骤失败也不应阻止后续只读生成。生成文件自身损坏或序列化失败仍需阻止损坏文件发布。
- Certitude 每次保留独立版本目录，快照表也持续追加：[generate_certitude.py:173](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/generate_certitude.py:173)、[certitude_store.py:287](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/certitude_store.py:287)。应先统计增长率，再制定旧版本/快照的保留期限，避免直接删除仍被用户页面引用的版本。
- 普通依赖仅设下限：[requirements.txt](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/requirements.txt)。为自动任务增加可重复安装的依赖约束和固定运行时；将分析、浏览器工具与核心采集依赖分组。
- 数据库 TLS 配置中出现 `rejectUnauthorized: false`：[data-service.js:38](C:/Users/cjyte/Documents/trae_projects/Geodataing/server/data-service.js:38)。需要结合连接串、驱动和服务商证书要求核查最终生效配置，再启用正确的证书校验；未进行线上握手验证。
- 审核登录和匿名提交的限流依赖平台配置。`REVIEW_SETUP.md` 已说明审核两个入口均需覆盖，仓库不能证明生产已配置或未配置。应核查覆盖路径、阈值和告警，不必为此立即新增数据库表。
- 发布 SSH 使用 `StrictHostKeyChecking no`：[crawl.yml:379](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/.github/workflows/crawl.yml:379)。后续可固定可信主机信息并收紧部署凭据权限。本次没有运行工作流或改变现有自动发布。
- 系列目录查询缺少结果上限：[data-service.js:274](C:/Users/cjyte/Documents/trae_projects/Geodataing/server/data-service.js:274)。先测量实际目录规模和响应体积，再选择缓存、分组静态结果或分页，避免在规模尚小时增加多余交互。
- 通知为“发送后标记”，邮件发送与数据库提交不是同一事务：[notify_series_requests.py:130](C:/Users/cjyte/Documents/trae_projects/Geo_data_v3.0/notify_series_requests.py:130)。发送成功但标记失败、重试或并发运行可能重复通知；可增加任务互斥、幂等标识及重试记录。未发送邮件验证。

验收：能从运行摘要看到各阶段更新时间、处理/失败/待重试数量、报表批次、耗时和体积；对生产配置有可复核的检查记录，日志不包含凭据。

## 实施顺序

| 阶段 | 工作 | 完成标准 |
| --- | --- | --- |
| A：阻止错误数据被当作成功 | 普通生成失败退出与原子批次；全排名口径/失败状态；浏览器错误缓存；owner GUID 保留 | 固定离线回归样本全部通过，失败注入不会替换有效数据 |
| B：恢复采集可持续推进 | 归档 limit/断点、失败退出；注册资料分字段重试；阶段状态汇总 | 小批连续运行会推进，失败能续跑且不会静默漏项 |
| C：控制网站和数据库开销 | 合并全排名查询、限制连接与超时；按需头像；路由拆包；健康检查 | 有冷/热请求和资源测量结果，查询与无关资源请求显著减少 |
| D：固定长期维护基础 | 跟踪离线测试和契约；单一服务端源码；只读生成与迁移分离；无障碍完善 | 新环境可重复构建/测试，改动能被自动检查，主要流程支持键盘 |

每阶段先在 `test/` 中验证不明确的原因，再改正式代码；可复用验证转入正式测试目录，一次性脚本删除。Git 备份、提交、push 和发布由用户手动执行或另行确认。

## 已确认的行为与实施边界

1. 归档日志保留现有每两周定时重新抓取的 Action。已从本地和 GitHub 主分支核验工作流存在及隔周判断；定时任务不沿用已完成藏点的本地断点。报告中的 limit/续跑问题只针对复用状态文件的运行，不应描述为现有定时任务长期漏查。
2. 上游采集失败时，后续继续使用数据库已有数据更新网站，不区分显示、不增加未更新区域或部分失败提示。运行记录可以保留失败数量，方便维护；这不构成网站展示要求。
3. 源数据较旧与生成文件损坏是两类情况：允许前者正常生成；生成/序列化失败、JSON 不完整及错误响应进入正常缓存仍需要修复。本轮已按该边界修改报表发布和工作流条件；具体完成项见本文开头的实施记录。
4. 查询超时限制按用户要求先跳过。TS/JS 唯一源码方案和地域补抓状态字段仍待选择，没有擅自切换源码或修改数据库结构。
