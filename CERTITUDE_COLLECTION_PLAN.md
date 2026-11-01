# Certitude 采集方案与验证记录

## 已确认的范围

- 每日静态交付遵循 `DATA_GENERATION_SPEC.md` 第 6 节，不新增实时 API。
- 以 Certitude dashboard 的发布时间倒序地区列表建立发现基线；只对列表中匹配项目数据库 Mystery（类型 8）的行读取详情并核验父级 GC。发现不覆盖 dashboard 当前不可见的历史页。
- 用户已确认：在现有 Neon 新增专用表；最终验证页可使用“来源 waypoint 与页面明确链接的父级 GC 完全一致，且无冲突”自动核验。
- 原始 checker 标识区分大小写，不通过改写前缀推导阶段或父级。不同编号且没有角色证据时保留 `unknown`。

## 匿名访问验证（2026-11-01）

先在 `test/` 编写临时探测脚本，再验证公开页面；不使用 Certitude Cookie、不提交答案。

| 样本 | 实际观测 | 用途 |
|---|---|---|
| `GC1EZB8` | 93 条公开名单记录，计数为 214 / 1830 | 普通页面、名单与尝试次数不同 |
| `W1C00FV` | 页面明确链接父级 `GCC00FV`，waypoint 保持原值 | 父级证据；不通过编号猜测角色 |
| `GC7B7VA` | 4387 条公开名单记录，计数为 6192 / 2064 | 大名单完整加载 |
| `GCBYFW4` | 成功与失败均为 0，公开名单为空 | 有效零值与采集失败的区别 |

计数会持续变化，上表是调研时的观测，不是固定业务数据。

名单在 HTML 的 `var solvers` 数组中，来源脚本 `showSolvers` 每次向页面渲染 1000 行，使用定时器继续处理同一数组，没有后续网络分页请求。因此采用 `requests` 获取 HTML，安全解析内嵌数据，不运行页面 JavaScript，也不需要安装浏览器。

同次页面中的 `b.success`、`b.error` 提供成功/失败计数；`showTimeline` 和名单提供 Unix 时间戳，转换为 UTC。页面的 `setTimezone` 单独保留。没有来源证据的答案类型、匹配方式或角色保持未知，不从图标推断。

官方来源：

- [Dashboard](https://www.certitudes.org/)：发现入口与可见列表，不能证明历史收录完整。
- [Questions](https://www.certitudes.org/questions)：匿名解题不进入具名名单，阶段可以串联；已归档验证页会从 Dashboard 隐藏，未发布数月的方案会自动删除。此次采集接受不覆盖已隐藏或未记录的历史页。
- [页面使用的公开脚本](https://static.certitudes.org/html/certitude.js)：完整数组的分批渲染方式。

## 数据流程

1. 匿名读取 Certitude 的实际 dashboard 表单和 `sort=dat&dir=desc` 发布时间列表，取得四个地区的真实筛选值。每区使用独立新会话；不使用 Geocaching 或 Certitude 登录 Cookie。
2. 首次发现遍历四区全部发布时间页，再按本地 Mystery（类型 8）GC 集合筛选行；不为其他缓存读取详情。后续使用每区已扫描的发布时间边界续扫，边界同秒继续读取；首次或断点扫描有页数上限并保存游标。检查点的 `candidateFingerprint` 记录 Mystery GC 集合；集合变化时重启发布时间列表基线，仅缓存元数据或状态变化不会重启。
3. 对匹配项目的 checker 详情使用匿名 HTTP 读取，以来源 waypoint 和页面明确父级链接核验映射。新发现、近期活动、失败或未完成项进入待处理队列；已完成且无变化的记录不重复入队。
4. `certitude_store.py` 将当前 checker 记录、真实观测快照及地区活动和发布时间扫描进度写入现有专用表。不保存答案、提示、失败提交正文或认证凭据。既有描述记录和 checker/snapshot 数据保留；不删除表或迁移数据。
5. 地区活动列表仍按实际表单筛选和 `sort=act&dir=desc` 每日刷新，发现近期活动变化并重试失败或未完成的 checker。每区每次最多读取 `CERTITUDE_ACTIVITY_PAGE_LIMIT` 页（默认 100；CLI `--activity-page-limit` 可覆盖）。
6. 发布时间扫描每区最多读取 `CERTITUDE_PUBLICATION_PAGE_LIMIT` 页（默认 100，0 表示不限页数；CLI `--publication-page-limit` 可覆盖）。`crawl-certitude` Action 输入 `publication_page_limit` 默认 100。到达上限保存真实游标；续跑时重新 POST 地区首页，再读取保存的游标。checker 待处理记录先持久化再推进游标。
7. 活动窗口仍根据请求起止时间、来源 `Date` 和缓存时间信息保守估计，边界采用包含式检查。dashboard 的 `c-act.act[t]` 是经过秒数，不是绝对时间；准确的 `latestActivityAt` 只从 checker 详情页读取。
8. 采集失败保留上次有效计数、名单及观测时间。只有本次实际取得有效数据时才写入对应观测或快照，不用旧值伪造历史。
9. `generate_certitude.py` 在只读一致性事务中读取主数据与已核验 checker；计算榜单、关系和 15 组范围。Found it 日志不再为 Certitude 静态数据联结。
10. 每批只写 manifest 和 15 个范围文件，共 16 个文件；写入新的不可变版本，重新读取 JSON 并校验字段、数量、排序和引用，最后原子切换 manifest。旧版本保留，独立于原有 `generated-at.json`。
11. 按 v1.4.1 精简静态字段：manifest 不再重复数量和覆盖信息，范围文件不再提供覆盖信息；缓存只提供摘要与五项统计，出题者榜单不再重复认证总数，关系边只保留双方及计数。内部仍保留完整规则、计数核验和关联 GC 去重集合。校验器只检查约定字段，忽略旧文件中的额外字段，schemaVersion 保持 2；生成不再读取只用于覆盖汇总的描述状态表。

## 覆盖和异常处理

- 当前自动采集以发布时间列表作为发现基线，不代表完整历史收录。Certitude 官方说明已归档方案会从 Dashboard 隐藏，且未发布数月的方案会自动删除；当前流程不扫描 Geocaching 描述，也不执行人工历史补录。
- 成功提交次数不等于公开人数；整题只按已核验最终规则计算。只有阶段证据时不能冒充整题完成。
- 各来源单独记录观察时间和最近尝试状态；缺失计数对不参与完整通过率计算。
- Found it 日志不参与 Certitude 静态数据生成；其他项目对日志的采集与报表不受此约定影响。
- 发布时间和活动索引共用 `certitude_activity_scans`，分别以 `published:<region>` 和 `regional:<region>` 保存进度；checker 的待处理状态在游标推进前保存。已扫描的发布时间边界只在完整遍历且时间已知时前移，同秒分页边界会继续读取。若本地 Mystery GC 候选集合变化，发布时间扫描从列表头重新开始，以覆盖新导入的更早发布时间缓存；其余元数据变化不重置扫描。活动窗口依据本轮请求起止时间、来源 HTTP `Date` 和缓存时间信息保守界定；失败项目可在未来窗口之外继续重试，不改变其已有快照时间。跳过详情抓取的 checker 不更新详情观测时间或快照。
- 对 Action 中记录的 3 个 `http_403` 来源做了只读复核：本机默认请求头返回 404，统一后的请求头及正式抓取/解析代码对 3 个链接均成功。原始 403 尚未在本机复现，仍需更新代码后在 runner 复测；不能把这项访问差异当作已证明的历史 403 原因。失败日志补充来源编号、主机/路径、HTTP 状态及有限响应特征，不输出查询参数、Cookie 或响应正文。
- 请求限速、超时、响应大小和重试有上限；日志只记录藏点编号、错误码及汇总，不输出页面正文或凭据。

## Action 接入与交付

`crawl-certitude` 作为独立 job 负责匿名采集并写入 Neon，不依赖或恢复/保存 Geocaching 加密 Cookie 状态；`crawl-caches` 和 `crawl-logs` 仍使用原有账号会话。`generate-data` 中的 Certitude 生成步骤只读取已保存数据。手动 `certitude` 模式可用 `publication_page_limit` 限制每地区发布时间列表页数，默认 100，0 表示不限页数；`generate` 模式不会启动 Certitude 爬虫，继续保留原有的用户注册时间补抓和生成发布流程。`both` 与定时运行按 `crawl-caches → crawl-logs → crawl-certitude → generate-data` 执行。采集失败或取消时跳过本轮 Certitude 生成，保留已发布版本。递归复制 Certitude 子目录，保留发布仓库已有历史版本。工作流改动本身不执行 Git 提交或 push。

前端后续从 manifest 读取固定批次路径；每批为 manifest 加 15 个范围文件，不生成逐题或合并详情文件。范围内保留题目摘要，玩家查看关联藏点时使用 `scope.caches` 和榜单中的 `cacheCodes`。发布环境宜将 manifest 设置为可重新验证、版本目录设置为长缓存。当前仓库只负责生成数据，不修改另一个仓库的页面或缓存配置。

此次验证覆盖离线契约、失败保留、原子发布以及少量真实匿名页面；不代表已经在 Neon 执行全量采集或完成前端接入。
