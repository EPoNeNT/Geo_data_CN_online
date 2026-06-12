# Geo Data v3.0

这是一个用于采集、整理并生成 Geocaching 中国区统计数据的 Python 项目。数据源主要来自 Geocaching 页面和接口，数据存储在 Neon PostgreSQL，最终输出静态 JSON 文件供前端项目使用。

本仓库不包含任何密钥、数据库连接串或 Cookie。运行前需要通过本地 `.env` 或 GitHub Actions Secrets 提供环境变量。

## 功能概览

- 增量抓取中国区 cache 数据，同步 cache 基础信息和状态。
- 根据地理边界将 cache 归属到城市。
- 增量抓取 cache 日志，识别 FTF，并回填用户 GUID。
- 为玩家排行榜补充头像 URL 缓存。
- 爬取用户的 Geocaching 注册时间及**首个找到 cache 所在国家**，写入 `"user"` 表。
- 回填 `caches.owner_guid` 和 `logs.user_guid`（通过详情页提取 + logbook API 分页获取）。
- 基于数据库生成前端使用的静态 JSON 数据。
- 通过 GitHub Actions 支持定时运行和手动运行。

## 主要脚本

| 脚本 | 作用 |
| --- | --- |
| `crawl_caches.py` | 抓取 cache 列表和 cache 状态，写入 `caches` 和 `crawl_progress`。 |
| `assign_cities.py` | 根据 `map_cities` 边界更新 `caches.city`。 |
| `crawl_logs.py` | 抓取 cache 日志，写入 `logs`，并更新 `logs_crawled_at` 等增量字段。 |
| `crawl_user_regdates.py` | 从 GUID 聚合出待抓取用户，爬取注册时间与首个找到的 cache 所在国家，写入 `"user"`。 |
| `generate_data.py` | 从数据库生成 `public/data/*.json` 静态数据。 |
| `seed_map_cities_once.py` | 一次性导入 `china_cities.json` 到 `map_cities`。 |
| `runtime_utils.py` | 公共运行时工具，包括环境变量校验、日志配置和登录失效判断。 |

### test/ 目录下的工具脚本

| 脚本 | 作用 |
| --- | --- |
| `test/backfill_guids.py` | 回填 `caches.owner_guid` 和 `logs.user_guid`。cache 全部填完后自动切换到 logs-only 模式。 |
| `test/fetch_first_find.py` | 补全 `"user"` 表中所有缺失的 `reg_place`（第一个找到 cache 的国家）。 |
| `test/fetch_user_regdates_once.py` | 一次性全量回填所有日志用户的注册时间。 |
| `test/debug_airq_timeout.py` | 诊断用户 profile 页面请求超时问题。 |
| `test/analyze_airq_html.py` | 分析用户 profile 页面体积异常原因。 |

## 目录结构

```text
.
├── .github/workflows/crawl.yml     # GitHub Actions 工作流
├── public/data/                    # 生成的静态 JSON，已被 git 忽略
├── sql/                            # Neon/Postgres 初始化 SQL
├── test/                           # 本地测试脚本、实验脚本和测试数据，已被 git 忽略
├── assign_cities.py
├── crawl_caches.py
├── crawl_logs.py
├── crawl_user_regdates.py
├── generate_data.py
├── seed_map_cities_once.py
├── runtime_utils.py
├── china_cities.json
├── .claude/                        # Claude Code 配置，已被 git 忽略
├── .gitignore
├── README.md
└── requirements.txt
```

## 环境要求

- Python 3.11+
- PostgreSQL / Neon
- PostGIS 扩展

安装依赖：

```bash
pip install -r requirements.txt
```

## 环境变量

必需：

| 变量 | 说明 |
| --- | --- |
| `DATABASE_URL` | Neon/PostgreSQL 连接串。 |
| `REG_COOKIE` 或 `GEOCOOKIE_NONPREMIUM` | 非 Premium 账号 Cookie，用于抓取普通数据。 |
| `GEOCOOKIE_PREMIUM` | Premium 账号 Cookie，用于抓取 Premium cache 相关数据及查看其他用户 finds。 |

可选：

| 变量 | 说明 |
| --- | --- |
| `GEOCACHING_MAP_VERSION` | 手动指定 Geocaching 地图接口 release 版本。 |
| `LOG_TO_FILE` | 设为 `1` / `true` 时输出脚本日志文件。 |
| `USER_REGDATE_TIMEOUT_SECONDS` | 注册时间请求超时时间（默认 45s）。 |
| `USER_REGDATE_MAX_RETRIES` | 注册时间爬取的单用户重试次数（默认 3）。 |
| `USER_REGDATE_DELAY_SECONDS` | 注册时间爬取用户间延迟（默认 1.6s）。 |
| `USER_REGDATE_BATCH_SIZE` | 注册时间结果批量写入大小（默认 50）。 |
| `USER_REGDATE_LIMIT` | 限制单次注册时间爬取的用户数量（默认 500）。 |
| `USER_REGDATE_STREAM_MAX_BYTES` | 注册时间页面流式读取上限（默认 200KB）。 |
| `AVATAR_MAX_RETRIES` | 头像爬取的单用户重试次数。 |
| `AVATAR_FETCH_DELAY_SECONDS` | 头像爬取用户间延迟。 |
| `AVATAR_REQUEST_TIMEOUT_SECONDS` | 头像请求超时时间。 |
| `FIRST_FIND_TIMEOUT_SECONDS` | 首个找到 cache 查询超时（默认 45s）。 |
| `FIRST_FIND_DELAY_SECONDS` | 首个找到 cache 查询用户间延迟（默认 1.0s）。 |

## 数据库表

项目依赖以下主要表：

| 表 | 说明 |
| --- | --- |
| `caches` | cache 主表，包含 GC code、坐标、类型、状态、城市、发布时间、FP、owner_guid 等信息。 |
| `logs` | cache 日志表，主要保存 Found it 日志、用户、访问日期、FTF 标记及 user_guid。 |
| `crawl_progress` | cache 网格抓取进度表。 |
| `map_cities` | 城市边界表，使用 PostGIS geometry。 |
| `user_avatars` | 玩家头像 URL 缓存表。 |
| `"user"` | 用户注册时间与首个找到 cache 国家缓存表。 |

`"user"` 表字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `user_name` | TEXT | 用户名（主键）。 |
| `guid` | TEXT | 用户 GUID。 |
| `registration_date` | DATE | Geocaching 注册日期。 |
| `reg_place` | TEXT | 第一个找到 cache 所在国家。 |
| `fetch_status` | TEXT | 抓取状态（`ok`、`not_found`、`request_failed` 等）。 |

初始化城市边界前，需要确保 PostGIS 可用。可参考 `sql/neon_city_setup.sql` 和 `seed_map_cities_once.py`。

## 本地运行

推荐完整顺序：

```bash
python crawl_caches.py
python assign_cities.py
python crawl_logs.py
python test/backfill_guids.py         # 回填 owner_guid 和 user_guid
python crawl_user_regdates.py         # 爬取注册时间 + 首个找到的国家
python generate_data.py
```

只生成静态数据：

```bash
python generate_data.py
```

小批量测试注册时间爬取：

```bash
python crawl_user_regdates.py --limit 20 --max-retries 1
```

回填 GUID：

```bash
# 检查状态
python test/backfill_guids.py --dry-run

# 回填 100 个 cache
python test/backfill_guids.py --limit 100

# 测试单个 cache
python test/backfill_guids.py --cache GC1001Y
```

补全 reg_place：

```bash
python test/fetch_first_find.py --limit 100
```

## 静态数据输出

`generate_data.py` 会输出：

| 文件 | 说明 |
| --- | --- |
| `public/data/overview.json` | 总览页数据。 |
| `public/data/player-rankings.json` | 玩家排行榜数据。 |
| `public/data/city-rankings.json` | 城市排行榜和城市详情数据。 |
| `public/data/generated-at.json` | 生成时间戳。 |

## GitHub Actions

工作流文件：`.github/workflows/crawl.yml`

支持：

- 定时任务：每天 UTC 20:00 运行。
- 手动运行：`mode=caches | logs | both | generate`。

当前流程：

- `caches`：运行 `crawl_caches.py`，然后运行 `assign_cities.py`。
- `logs`：运行 `crawl_logs.py`。
- `both`：依次运行 cache 抓取、城市归属、日志抓取、用户注册时间补抓、静态数据生成。
- `generate`：只生成静态数据。
- 定时任务：运行完整链路，并包含 `crawl_user_regdates.py`。

需要在 GitHub Secrets 中配置：

- `DATABASE_URL`
- `GEOCOOKIE_NONPREMIUM`
- `GEOCOOKIE_PREMIUM`
- `GEODATAING_DEPLOY_KEY`

## 开发约定

- 主目录下的脚本是正式版本，用于 GitHub Actions。
- `test/` 目录用于本地测试、临时验证和实验脚本。
- 对 `generate_data.py` 的统计口径修改，应先在 `test/generate_data.py` 和相关测试中验证，再同步到主目录脚本。
- `public/`、`test/`、`.env`、`.claude/` 已被 git 忽略。
- 不要将数据库连接串、Cookie 或账号信息写入代码、README、日志或 Issue。
- Cookie 环境变量预处理时会自动最小化（仅保留 `gspkauth` 字段），避免 header 过大。

## 常见问题

### Geocaching 返回登录页或 403

通常是 Cookie 失效。更新本地 `.env` 或 GitHub Secrets 中的 Cookie。

### 某个用户请求超时或耗时很长

部分用户的 profile 页面体积异常大（About 栏目嵌入大量富文本），可能导致默认超时不够。`crawl_user_regdates.py` 已使用流式读取（只取前 200KB）来避免此问题。若仍有超时，可增大 `USER_REGDATE_TIMEOUT_SECONDS`。

### 注册时间补抓运行很久

`logs` 中用户数量较大时，首次补抓会很慢。可以先用：

```bash
python crawl_user_regdates.py --limit 100
```

确认行为正常后再放开限制。

### 用户首个找到 cache（reg_place）为 NULL

可能原因：
- 用户 finds 设为私密（Geocaching 页面显示 "This content is private"）。
- 用户名含 `+` 号（已处理，需用 `%252B` 双重编码）。
- 用户是 cache owner 但从未找过 cache（finds=0）。

### `assign_cities.py` 报 `map_cities` 为空

说明数据库中尚未导入城市边界。先运行 `seed_map_cities_once.py` 或使用对应一次性导入脚本。
