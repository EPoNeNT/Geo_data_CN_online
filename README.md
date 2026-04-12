# Geo Data CN Online

Geocaching 中国区数据采集项目，基于 Python + Neon(PostgreSQL)。

项目核心能力：
- 增量抓取缓存点（cache）并同步状态
- 增量抓取缓存日志（Found it）
- 在数据库内进行城市归属（`caches.city`）
- 支持 GitHub Actions 定时/手动运行

本文档不包含任何隐私信息（如真实数据库连接串、账号 Cookie）。

## 1. 功能与模块

### `crawl_caches.py`
- 按网格抓取 Geocaching 地图数据
- 自动检测地图 release 版本
- 将缓存点增量写入 `caches`（upsert）
- 对大网格自动四分裂并写入 `crawl_progress`
- 对潜在归档缓存做状态确认

### `assign_cities.py`
- 在 Postgres 中执行城市划分 SQL
- 港澳台固定映射：`香港 / 澳门 / 台湾`
- 中国大陆根据 `map_cities` 做最近边界匹配
- 只处理 `city IS NULL` 且有经纬度的数据

### `crawl_logs.py`
- 分 Premium / Non-premium 两类 Cookie 抓取日志
- 只写入 `Found it` 日志到 `logs`
- 使用 `logs_crawled_at` 与 `last_found_date` 做增量筛选
- Premium 组支持坐标回填

### `runtime_utils.py`
- 环境变量校验
- 统一日志配置
- 登录失效检测（登录页/重定向识别）

## 2. 实现思路

### 缓存抓取
- 以 `crawl_progress` 驱动抓取队列，逐网格扫描。
- 若网格返回结果过多（>800）则拆分子网格继续抓取，降低漏抓风险。
- 对同一 `code` 使用 `ON CONFLICT` 更新，保证幂等和增量效率。

### 日志抓取
- 只处理非归档缓存。
- 过滤规则：
  - `logs_crawled_at IS NULL`（从未抓取）
  - 或 `last_found_date >= logs_crawled_at`（存在新活动）
- 每批写入后更新 `logs_crawled_at`，形成闭环增量。

### 城市划分
- 城市边界保存在数据库 `map_cities`。
- 使用 PostGIS 距离排序（含 KNN `<->`）匹配最近行政边界。
- 港澳台不走空间匹配，直接按国家字段映射中文名。

## 3. 目录结构

```text
.
├─ crawl_caches.py
├─ assign_cities.py
├─ crawl_logs.py
├─ runtime_utils.py
├─ china_cities.json
├─ requirements.txt
└─ .github/workflows/
   └─ crawl.yml
```

## 4. 环境要求

- Python 3.11+
- PostgreSQL（Neon）并启用 PostGIS

安装依赖：

```bash
pip install -r requirements.txt
```

## 5. 环境变量

必需：
- `DATABASE_URL`
- `GEOCOOKIE_NONPREMIUM`
- `GEOCOOKIE_PREMIUM`

可选：
- `GEOCACHING_MAP_VERSION`（手动指定地图 release，排障用）
- `LOG_TO_FILE`（`1/true` 启用文件日志）

示例（占位符）：

```bash
export DATABASE_URL="postgresql://<user>:<password>@<host>/<db>?sslmode=require"
export GEOCOOKIE_NONPREMIUM="<cookie>"
export GEOCOOKIE_PREMIUM="<cookie>"
```

## 6. 数据库说明

业务上需要以下表：
- `caches`
- `logs`
- `crawl_progress`
- `map_cities`

说明：
- `map_cities` 需要预先准备好城市边界数据。
- `assign_cities.py` 默认只做城市更新，不负责在线导入边界数据。

## 7. 使用方法

本地推荐顺序：

```bash
python crawl_caches.py
python assign_cities.py
python crawl_logs.py
```

## 8. GitHub Actions

工作流：`.github/workflows/crawl.yml`

支持：
- 定时执行（每天）
- 手动触发：`mode=caches | logs | both`

需要在仓库 Secrets 配置：
- `DATABASE_URL`
- `GEOCOOKIE_NONPREMIUM`
- `GEOCOOKIE_PREMIUM`

## 9. 常见问题

- `assign_cities.py` 报 `map_cities` 为空：
  - 说明数据库里没有边界数据，先准备 `map_cities` 后再运行。
- Geocaching 接口返回登录页或 403：
  - Cookie 失效，更新对应 Secrets。
- 地图接口 404：
  - release 可能变更，可临时设置 `GEOCACHING_MAP_VERSION` 排障。

## 10. 安全建议

- 不要在仓库、日志、Issue 中暴露：
  - 数据库连接串
  - Cookie
  - 账号信息
- 建议定期轮换数据库凭据和 Cookie。
