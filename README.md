# Geo Data v3.0

这是一个用于采集、整理并生成 Geocaching 中国区统计数据的 Python 项目。数据源主要来自 Geocaching 页面和接口，数据存储在 Neon PostgreSQL，最终输出静态 JSON 文件供前端项目使用。

本仓库不包含任何密钥、数据库连接串或 Cookie。运行前需要通过本地 `.env` 或 GitHub Actions Secrets 提供环境变量。

## 功能概览

- 增量抓取中国区 cache 数据，并同步 cache 的基础信息和状态。
- 根据地理边界将 cache 归属到城市。
- 增量抓取 cache 的 Found it 日志，并识别 FTF。
- 为玩家排行榜补充头像 URL 缓存。
- 补抓 logs 中用户的 Geocaching 注册时间，并写入 `"user"` 表。
- 基于数据库生成前端使用的静态 JSON 数据。
- 通过 GitHub Actions 支持定时运行和手动运行。

## 主要脚本

| 脚本 | 作用 |
| --- | --- |
| `crawl_caches.py` | 抓取 cache 列表和 cache 状态，写入 `caches` 和 `crawl_progress`。 |
| `assign_cities.py` | 根据 `map_cities` 边界更新 `caches.city`。 |
| `crawl_logs.py` | 抓取 cache 日志，写入 `logs`，并更新 `logs_crawled_at` 等增量字段。 |
| `crawl_user_regdates.py` | 从 `logs.user_name` 找出 `"user"` 表中缺失或非 `ok` 的用户，爬取注册时间并写入 `"user"`。 |
| `generate_data.py` | 从数据库生成 `public/data/*.json` 静态数据。 |
| `seed_map_cities_once.py` | 一次性导入 `china_cities.json` 到 `map_cities`。 |
| `runtime_utils.py` | 公共运行时工具，包括环境变量校验、日志配置和登录失效判断。 |

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
├── DATA_GENERATION_SPEC.md         # 数据结构与统计口径说明，已被 git 忽略
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
| `GEOCOOKIE_NONPREMIUM` | 非 Premium 账号 Cookie，用于抓取普通数据。 |
| `GEOCOOKIE_PREMIUM` | Premium 账号 Cookie，用于抓取 Premium cache 相关数据。 |

可选：

| 变量 | 说明 |
| --- | --- |
| `GEOCACHING_COOKIE` / `GEOCACHING_COOKIES` | 用户头像或注册时间爬取可使用的通用 Geocaching Cookie。未设置时会回退到 `GEOCOOKIE_NONPREMIUM` 或 `GEOCOOKIE_PREMIUM`。 |
| `GEOCACHING_MAP_VERSION` | 手动指定 Geocaching 地图接口 release 版本。 |
| `LOG_TO_FILE` | 设为 `1` / `true` 时输出脚本日志文件。 |
| `AVATAR_MAX_RETRIES` | 头像爬取的单用户重试次数。 |
| `AVATAR_FETCH_DELAY_SECONDS` | 头像爬取用户间延迟。 |
| `AVATAR_REQUEST_TIMEOUT_SECONDS` | 头像请求超时时间。 |
| `USER_REGDATE_MAX_RETRIES` | 注册时间爬取的单用户重试次数。 |
| `USER_REGDATE_TIMEOUT_SECONDS` | 注册时间请求超时时间。 |
| `USER_REGDATE_DELAY_SECONDS` | 注册时间爬取用户间延迟。 |
| `USER_REGDATE_BATCH_SIZE` | 注册时间结果批量写入大小。 |
| `USER_REGDATE_LIMIT` | 限制单次注册时间爬取的用户数量。 |

## 数据库表

项目依赖以下主要表：

| 表 | 说明 |
| --- | --- |
| `caches` | cache 主表，包含 GC code、坐标、类型、状态、城市、发布时间、FP 等信息。 |
| `logs` | cache 日志表，主要保存 Found it 日志、用户、访问日期和 FTF 标记。 |
| `crawl_progress` | cache 网格抓取进度表。 |
| `map_cities` | 城市边界表，使用 PostGIS geometry。 |
| `user_avatars` | 玩家头像 URL 缓存表。 |
| `"user"` | 用户注册时间缓存表。表名需要加双引号，因为 `user` 是 PostgreSQL 关键字。 |

初始化城市边界前，需要确保 PostGIS 可用。可参考 `sql/neon_city_setup.sql` 和 `seed_map_cities_once.py`。

## 本地运行

推荐完整顺序：

```bash
python crawl_caches.py
python assign_cities.py
python crawl_logs.py
python crawl_user_regdates.py
python generate_data.py
```

只生成静态数据：

```bash
python generate_data.py
```

只检查注册时间爬取需要处理多少用户：

```bash
python crawl_user_regdates.py --dry-run
```

小批量测试注册时间爬取：

```bash
python crawl_user_regdates.py --limit 20 --max-retries 1
```

## 静态数据输出

`generate_data.py` 会输出：

| 文件 | 说明 |
| --- | --- |
| `public/data/overview.json` | 总览页数据。 |
| `public/data/player-rankings.json` | 玩家排行榜数据。 |
| `public/data/city-rankings.json` | 城市排行榜和城市详情数据。 |
| `public/data/generated-at.json` | 生成时间戳。 |

详细字段结构和统计口径以 `DATA_GENERATION_SPEC.md` 为准。

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
- `public/`、`test/`、`.env`、`DATA_GENERATION_SPEC.md` 已被 git 忽略。
- 不要将数据库连接串、Cookie 或账号信息写入代码、README、日志或 Issue。

## 常见问题

### Geocaching 返回登录页或 403

通常是 Cookie 失效。更新本地 `.env` 或 GitHub Secrets 中的 Cookie。

### `assign_cities.py` 报 `map_cities` 为空

说明数据库中尚未导入城市边界。先运行 `seed_map_cities_once.py` 或使用对应一次性导入脚本。

### 注册时间补抓运行很久

`logs` 中用户数量较大时，首次补抓会很慢。可以先用：

```bash
python crawl_user_regdates.py --limit 100
```

确认行为正常后再放开限制。

### 生成数据和前端字段不一致

先检查 `DATA_GENERATION_SPEC.md` 是否已经同步到最新，再检查 `generate_data.py` 和前端读取逻辑是否同时更新。
