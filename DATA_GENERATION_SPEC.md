# GitHub Actions 每日数据生成脚本 - 数据需求清单

## 概述

本文档定义了每日定时任务需要生成的所有 JSON 静态数据文件及其结构。这些文件将供前端页面直接读取，减少对数据库的实时查询压力。

## 输出目录结构

```
public/data/
├── overview.json              # 总览页数据
├── player-rankings.json       # 玩家排行榜数据
├── city-rankings.json         # 城市排名数据
└── generated-at.json          # 数据生成时间戳
```

---

## 1. 总览页数据 (`overview.json`)

**对应 API**: `/api/overview`
**使用页面**: `/` (总览页)
**更新频率**: 每日一次

### 完整 JSON 结构

```json
{
  "generatedAt": "2026-04-14T00:00:00.000Z",
  
  "summary": {
    "metrics": {
      "totalCaches": 48502,
      "totalFinders": 12842,
      "totalOwners": 3211,
      "totalLogs": 842105
    },
    "yearlyTrend": [
      {
        "year": "2012",
        "total": 400,
        "ytdCache": 120
      },
      {
        "year": "2013",
        "total": 600,
        "ytdCache": 180
      }
    ],
    "dtMatrix": [
      {
        "row": 0,
        "col": 0,
        "difficulty": 1.0,
        "terrain": 1.0,
        "count": 150
      }
    ]
  },

  "regions": [
    {
      "key": "china",
      "name": "中国大陆",
      "totalCaches": 28402,
      "percentage": 58.6
    },
    {
      "key": "taiwan",
      "name": "台湾",
      "totalCaches": 12211,
      "percentage": 25.2
    },
    {
      "key": "hong-kong",
      "name": "香港",
      "totalCaches": 6842,
      "percentage": 14.1
    },
    {
      "key": "macao",
      "name": "澳门",
      "totalCaches": 1047,
      "percentage": 2.2
    }
  ],

  "regionScopes": {
    "china": {
      "metrics": { ... },
      "yearlyTrend": [ ... ],
      "dtMatrix": [ ... ]
    },
    "taiwan": { ... },
    "hong-kong": { ... },
    "macao": { ... }
  }
}
```

### 字段说明

#### `summary.metrics` 对象
| 字段名 | 类型 | 说明 | SQL 来源 |
|--------|------|------|----------|
| `totalCaches` | number | 该区域藏宝总数 | `SELECT COUNT(*) FROM caches` |
| `totalFinders` | number | 寻宝玩家数（logs 中去重的 user_name） | `SELECT COUNT(DISTINCT user_name) FROM logs` |
| `totalOwners` | number | 藏宝创建者数（caches 中去重的 owner_username） | `SELECT COUNT(DISTINCT owner_username) FROM caches` |
| `totalLogs` | number | Log 记录总数 | `SELECT COUNT(*) FROM logs` |

#### `summary.yearlyTrend` 数组
| 字段名 | 类型 | 说明 |
|--------|------|------|
| `year` | string | 年份字符串，如 `"2024"` |
| `total` | number | 该年新增藏宝数量 |
| `ytdCache` | number | 该年截至今天同一天（YTD, Year-to-Date）的累计缓存数量 |

**注意**:
- 年份范围：从数据库中最早的 `placed_date` 年份到当前年份
- 必须包含所有中间年份，即使某年没有数据也要返回 `{ year: "xxxx", total: 0, ytdCache: 0 }`
- **ytdCache 字段含义**：例如今天是 4 月 14 日，则该值为每一年从 1 月 1 日到 4 月 14 日的累计 cache 数量
- 这表示"截至当前日期的年度进度"，用于对比不同年份在同一时间点的累计增长情况
- **前端显示限制**：前端页面只会显示包含当前年份在内的最近15年数据，实现动态滚动窗口效果

#### `summary.dtMatrix` 数组
| 字段名 | 类型 | 说明 |
|--------|------|------|
| `row` | number | 矩阵行索引 (0-8)，对应 difficulty |
| `col` | number | 矩阵列索引 (0-8)，对应 terrain |
| `difficulty` | number | 难度值，取值范围: [1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5] |
| `terrain` | number | 地形值，取值范围同上 |
| `count` | number | 该 D/T 组合下的缓存数量 |

**注意**: 
- 必须是 9×9 = 81 个元素的数组
- 即使 count=0 的组合也要包含在内
- 排序顺序：先按 row 升序，再按 col 升序

#### `regions` 数组
固定顺序：
1. china (中国大陆)
2. taiwan (台湾)
3. hong-kong (香港)
4. macao (澳门)

每个区域对象的 `regionScopes` 结构与 `summary` 完全相同。

---

## 2. 玩家排行榜数据 (`player-rankings.json`)

**对应 API**: `/api/players`
**使用页面**: `/players` (排行榜页)
**更新频率**: 每日一次

### 完整 JSON 结构

```json
{
  "generatedAt": "2026-04-14T00:00:00.000Z",

  "rankings": {
    "hides": {
      "30d": [
        {
          "rank": 1,
          "name": "玩家A",
          "score": 500,
          "trend": "up",
          "trendDelta": 15
        }
      ],
      "ytd": [ ... ],
      "all": [ ... ]
    },
    
    "finds": {
      "30d": [ ... ],
      "ytd": [ ... ],
      "all": [ ... ]
    },
    
    "favorites": {
      "30d": [ ... ],
      "ytd": [ ... ],
      "all": [ ... ]
    },
    
    "logs": {
      "30d": [ ... ],
      "ytd": [ ... ],
      "all": [ ... ]
    }
  },

  "communityStats": {
    "activePlayers": 12842,
    "activePlayersGrowthPct": 12.4,
    "totalCaches": 48502,
    "foundCacheCoveragePct": 75.0
  }
}
```

### 字段说明

#### `rankings` 对象结构

**第一层 key** (排名类型):
- `hides`: 藏宝排行（按 owner_username 分组 COUNT）
- `finds`: 寻宝排行（按 logs.user_name 去重 gc_code 计数）
- `favorites`: FP 数排行（按 owner_username 分组 SUM favorite_points）
- **`logs`: 获得Logs数排行**（统计玩家所藏的宝被其他玩家log的总次数）

**第二层 key** (时间范围):
- `30d`: 最近 30 天
- `ytd`: 今年至今 (Year to Date)
- `all`: 所有时间

#### 排行榜条目对象
| 字段名 | 类型 | 说明 |
|--------|------|------|
| `rank` | number | 显示排名（考虑并列后的排名） |
| `name` | string | 用户名/城市名 |
| `score` | number | 分数（根据排名类型不同含义） |
| `trend` | string | 趋势: `"up"` / `"down"` / `"flat"` |
| `trendDelta` | number | 与上一周期相比的**排名变化量** |

**注意**:
- 每个排行榜默认返回 **30 条**记录
- **不显示分数为0的记录**
- **并列排名规则**:
  - 分数相同的条目显示相同的排名序号
  - 下一组不同分数的条目跳过被占用的排名序号
  - **示例**: 第1名(100分), 第2名(90分), 第2名(90分), 第4名(80分) - 跳过第3名
- **金银铜颜色规则** (前端渲染):
  - 排名=1: 金色 (`text-yellow-500`)
  - 排名=2: 银色 (`text-slate-400`)
  - 排名=3: 铜色 (`text-amber-600`)
  - 并列情况: 如果有多个第2名，都显示银色；如果有多个第3名，都显示铜色
- **超限显示规则**:
  - 正常情况下最多显示30条
  - 如果因为并列导致第30名附近有同分的情况，则将所有同分的条目全部显示
  - **示例**: 第29名(50分), 第29名(50分), 第29名(50分) → 显示31条记录
- **trend 基于**排名**变化计算**，而非分数变化
  - `trend = "up"`: 排名上升（例如从第4名到第2名）
  - `trend = "down"`: 排名下降（例如从第2名到第4名）
  - `trend = "flat"`: 排名不变
- `trendDelta`: 排名的绝对变化值（正数表示排名提升）
- 新上榜的玩家/城市（上一周期不在排行榜中）: `trend = "up"`, `trendDelta` = 当前排名

#### `communityStats` 对象
| 字段名 | 类型 | 说明 | 计算方式 |
|--------|------|------|----------|
| `activePlayers` | number | 近30天活跃玩家数 | `COUNT(DISTINCT user_name)` WHERE visited >= 当前日期 - 30天 |
| `activePlayersGrowthPct` | number | 较上月增长百分比 | `(本月活跃 - 上月活跃) / 上月活跃 * 100` |
| `totalCaches` | number | 缓存总数 | `COUNT(*) FROM caches` |
| `foundCacheCoveragePct` | number | 已被发现缓存占比 | `被发现的去重 gc_code 数 / 总缓存数 * 100` |

---

## 3. 城市排名数据 (`city-rankings.json`)

**对应 API**: `/api/cities`
**使用页面**: `/cities` (城市排名页)
**更新频率**: 每日一次

### 完整 JSON 结构

```json
{
  "generatedAt": "2026-04-14T00:00:00.000Z",

  "rankings": {
    "hides": {
      "30d": [
        {
          "rank": 1,
          "name": "上海",
          "subtitle": "中国",
          "score": 4892
        }
      ],
      "ytd": [ ... ],
      "all": [ ... ]
    },
    
    "finds": {
      "30d": [ ... ],
      "ytd": [ ... ],
      "all": [ ... ]
    },
    
    "favorites": {
      "30d": [ ... ],
      "ytd": [ ... ],
      "all": [ ... ]
    },
    
    "logs": {
      "30d": [ ... ],
      "ytd": [ ... ],
      "all": [ ... ]
    }
  },

  "cacheTrend": {
    "averageGrowthPct": 24.8,
    "bars": [
      {
        "year": "2019",
        "count": 1200
      },
      {
        "year": "2020",
        "count": 1500
      }
    ]
  },

  "dtMatrix": [
    {
      "row": 0,
      "col": 0,
      "difficulty": 1.0,
      "terrain": 1.0,
      "count": 200
    }
  ]
}
```

### 字段说明

#### `rankings` 对象结构

与玩家排行榜相同的第一层和第二层 key。

#### 城市排行榜条目对象
| 字段名 | 类型 | 说明 |
|--------|------|------|
| `rank` | number | 显示排名（考虑并列后的排名） |
| `name` | string | 城市名称（优先显示 city 字段，为空则显示 country） |
| `subtitle` | string | 国家/地区名称 |
| `score` | number | 分数 |
| `trend` | string | 趋势: `"up"` / `"down"` / `"flat"` |
| `trendDelta` | number | 与上一周期相比的**排名变化量** |

**注意**:
- 每个排行榜默认返回 **20 条**记录
- **不显示分数为0的记录**
- **并列排名规则**: 与玩家排行榜相同（见上方说明）
- **超限显示规则**: 与玩家排行榜相同（见上方说明）
- 城市名称来源：`COALESCE(NULLIF(TRIM(city), ''), country)`
- 只包含 city 或 country 不为空的记录

#### `cacheTrend` 对象
| 字段名 | 类型 | 说明 |
|--------|------|------|
| `averageGrowthPct` | number | 平均年增长率百分比（保留1位小数） |
| `bars` | array | 年度数据条形图数据 |

**`bars` 数组元素**:
| 字段名 | 类型 | 说明 |
|--------|------|------|
| `year` | string | 年份 |
| `count` | number | 该年新增缓存数 |

**注意**:
- 显示最近 7 年的数据（从当前年份往前推6年）
- 例如当前是 2026 年，则显示 2020-2026
- 必须包含所有中间年份，无数据的年份 count 为 0

#### `dtMatrix` 数组

结构与总览页的 dtMatrix 完全相同（81 个元素，9×9 矩阵）。

---

## 4. 数据生成时间戳 (`generated-at.json`)

```json
{
  "generatedAt": "2026-04-14T00:00:00.000Z",
  "version": "1.0.0"
}
```

**用途**:
- 前端可用来判断数据是否过期
- 可用于显示"最后更新时间"

---

## SQL 查询参考

以下是对应每个数据文件的完整 SQL 查询，可直接用于你的脚本：

### 4.1 总览页 - Summary Metrics

```sql
WITH cache_scope AS (
  SELECT * FROM caches c
  -- WHERE c.country = 'China'  -- 如果需要特定区域
),
log_scope AS (
  SELECT l.* 
  FROM logs l
  JOIN caches c ON c.code = l.gc_code
  -- WHERE c.country = 'China'
)
SELECT
  (SELECT COUNT(*)::int FROM cache_scope) AS total_caches,
  (SELECT COUNT(DISTINCT owner_username)::int FROM cache_scope WHERE owner_username IS NOT NULL AND owner_username <> '') AS total_owners,
  (SELECT COUNT(DISTINCT user_name)::int FROM log_scope WHERE user_name IS NOT NULL AND user_name <> '') AS total_finders,
  (SELECT COUNT(*)::int FROM log_scope) AS total_logs;
```

### 4.2 总览页 - Yearly Trend

```sql
WITH years AS (
  SELECT generate_series(
    COALESCE(
      (SELECT MIN(EXTRACT(YEAR FROM placed_date)::int) FROM caches c WHERE placed_date IS NOT NULL),
      EXTRACT(YEAR FROM CURRENT_DATE)::int
    ),
    EXTRACT(YEAR FROM CURRENT_DATE)::int
  ) AS year
),
counts AS (
  SELECT
    EXTRACT(YEAR FROM placed_date)::int AS year,
    COUNT(*)::int AS total,
    COUNT(*) FILTER (
      WHERE placed_date::date <= 
        (date_trunc('year', make_date(EXTRACT(YEAR FROM placed_date)::int, 1, 1))::date 
         + (CURRENT_DATE - date_trunc('year', CURRENT_DATE)::date))
    )::int AS ytd_cache
  FROM caches c
  WHERE placed_date IS NOT NULL
  -- AND c.country = 'China'  -- 如果需要特定区域
  GROUP BY 1
)
SELECT
  years.year::text AS year,
  COALESCE(counts.total, 0)::int AS total,
  COALESCE(counts.ytd_cache, 0)::int AS ytd_cache
FROM years
LEFT JOIN counts USING (year)
ORDER BY years.year;
```

**YTD Cache 字段说明**:
- `ytd_cache` 计算的是每一年从年初到"今天同一天"的累计缓存数量
- 例如：如果今天是 4 月 14 日，则统计每一年 1 月 1 日 至 4 月 14 日的 cache 数量
- 这用于对比不同年份在同一时间点的增长进度

### 4.3 总览页 / 城市排名 - D/T Matrix

```sql
SELECT
  c.difficulty::float8 AS difficulty,
  c.terrain::float8 AS terrain,
  COUNT(*)::int AS count
FROM caches c
WHERE c.difficulty IN (1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5)
  AND c.terrain IN (1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5)
  -- AND c.country = 'China'  -- 如果需要特定区域
GROUP BY c.difficulty, c.terrain
ORDER BY c.difficulty, c.terrain;
```

**重要**: 结果集可能不足 81 条，需要在代码中补全缺失的组合（count 设为 0）。

### 4.4 玩家排行榜 - Hides (藏宝排行)

```sql
-- 30天版本
SELECT owner_username AS name, COUNT(*)::int AS score
FROM caches
WHERE owner_username IS NOT NULL AND owner_username <> ''
  AND placed_date >= CURRENT_DATE - INTERVAL '30 day'
GROUP BY owner_username
ORDER BY score DESC, owner_username ASC
LIMIT 30;

-- YTD 版本
SELECT owner_username AS name, COUNT(*)::int AS score
FROM caches
WHERE owner_username IS NOT NULL AND owner_username <> ''
  AND placed_date >= date_trunc('year', CURRENT_DATE)::date
GROUP BY owner_username
ORDER BY score DESC, owner_username ASC
LIMIT 30;

-- 所有时间版本
SELECT owner_username AS name, COUNT(*)::int AS score
FROM caches
WHERE owner_username IS NOT NULL AND owner_username <> ''
GROUP BY owner_username
ORDER BY score DESC, owner_username ASC
LIMIT 30;
```

### 4.5 玩家排行榜 - Finds (寻宝排行)

```sql
-- 30天版本
SELECT user_name AS name, COUNT(DISTINCT gc_code)::int AS score
FROM logs
WHERE user_name IS NOT NULL AND user_name <> ''
  AND visited >= CURRENT_DATE - INTERVAL '30 day'
GROUP BY user_name
ORDER BY score DESC, user_name ASC
LIMIT 30;

-- YTD 和 All 类似，修改日期条件即可
```

### 4.6 玩家排行榜 - Favorites (FP 排行)

```sql
SELECT owner_username AS name, COALESCE(SUM(favorite_points), 0)::int AS score
FROM caches
WHERE owner_username IS NOT NULL AND owner_username <> ''
  AND placed_date >= CURRENT_DATE - INTERVAL '30 day'  -- 根据时间范围调整
GROUP BY owner_username
ORDER BY score DESC, owner_username ASC
LIMIT 30;
```

### 4.7 玩家排行榜 - 获得Logs数排行

**说明**: 统计每个玩家所藏的宝被其他玩家log的总次数。时间筛选的是这些log的visited日期，而不是cache的放置日期。

```sql
-- 30天版本
SELECT
  c.owner_username AS name,
  COUNT(l.*)::int AS score
FROM caches c
JOIN logs l ON l.gc_code = c.code
WHERE c.owner_username IS NOT NULL AND c.owner_username <> ''
  AND l.visited >= CURRENT_DATE - INTERVAL '30 day'
GROUP BY c.owner_username
ORDER BY score DESC, c.owner_username ASC
LIMIT 30;

-- YTD 版本
SELECT
  c.owner_username AS name,
  COUNT(l.*)::int AS score
FROM caches c
JOIN logs l ON l.gc_code = c.code
WHERE c.owner_username IS NOT NULL AND c.owner_username <> ''
  AND l.visited >= date_trunc('year', CURRENT_DATE)::date
GROUP BY c.owner_username
ORDER BY score DESC, c.owner_username ASC
LIMIT 30;

-- 所有时间版本
SELECT
  c.owner_username AS name,
  COUNT(l.*)::int AS score
FROM caches c
JOIN logs l ON l.gc_code = c.code
WHERE c.owner_username IS NOT NULL AND c.owner_username <> ''
GROUP BY c.owner_username
ORDER BY score DESC, c.owner_username ASC
LIMIT 30;
```

### 4.8 城市排行榜

```sql
-- Hides (藏宝排行)
SELECT 
  COALESCE(NULLIF(TRIM(city), ''), country) AS name,
  country AS subtitle,
  COUNT(*)::int AS score
FROM caches
WHERE COALESCE(NULLIF(TRIM(city), ''), country) IS NOT NULL
  AND placed_date >= CURRENT_DATE - INTERVAL '30 day'  -- 根据时间范围调整
GROUP BY name, subtitle
ORDER BY score DESC, name ASC
LIMIT 20;

-- Finds (寻宝排行) - 需要 JOIN logs 表
SELECT 
  COALESCE(NULLIF(TRIM(c.city), ''), c.country) AS name,
  c.country AS subtitle,
  COUNT(DISTINCT l.user_name)::int AS score
FROM logs l
JOIN caches c ON c.code = l.gc_code
WHERE COALESCE(NULLIF(TRIM(c.city), ''), c.country) IS NOT NULL
  AND l.visited >= CURRENT_DATE - INTERVAL '30 day'  -- 根据时间范围调整
GROUP BY name, subtitle
ORDER BY score DESC, name ASC
LIMIT 20;

-- Favorites (FP 排行)
SELECT
  COALESCE(NULLIF(TRIM(c.city)), c.country) AS name,
  c.country AS subtitle,
  COALESCE(SUM(c.favorite_points), 0)::int AS score
FROM caches c
WHERE COALESCE(NULLIF(TRIM(c.city)), c.country) IS NOT NULL
  AND c.owner_username IS NOT NULL AND c.owner_username <> ''
  AND c.placed_date >= CURRENT_DATE - INTERVAL '30 day'  -- 根据时间范围调整
GROUP BY name, subtitle
ORDER BY score DESC, name ASC
LIMIT 20;

-- 获得Logs数排行 - 统计该城市藏宝被其他玩家log的总次数
SELECT
  COALESCE(NULLIF(TRIM(c.city)), c.country) AS name,
  c.country AS subtitle,
  COUNT(l.*)::int AS score
FROM caches c
JOIN logs l ON l.gc_code = c.code
WHERE COALESCE(NULLIF(TRIM(c.city)), c.country) IS NOT NULL
  AND l.visited >= CURRENT_DATE - INTERVAL '30 day'  -- 根据时间范围调整，筛选的是log的visited日期
GROUP BY name, subtitle
ORDER BY score DESC, name ASC
LIMIT 20;
```

### 4.9 城市排名 - Cache Trend (趋势图)

```sql
WITH years AS (
  SELECT generate_series(
    EXTRACT(YEAR FROM CURRENT_DATE)::int - 6,
    EXTRACT(YEAR FROM CURRENT_DATE)::int
  ) AS year
),
counts AS (
  SELECT EXTRACT(YEAR FROM placed_date)::int AS year, COUNT(*)::int AS count
  FROM caches
  WHERE placed_date IS NOT NULL
    AND EXTRACT(YEAR FROM placed_date)::int >= EXTRACT(YEAR FROM CURRENT_DATE)::int - 6
  GROUP BY 1
)
SELECT years.year::text AS year, COALESCE(counts.count, 0)::int AS count
FROM years
LEFT JOIN counts USING (year)
ORDER BY years.year;
```

### 4.10 Community Stats

```sql
-- 活跃玩家数（近30天）
SELECT COUNT(DISTINCT user_name)::int AS count
FROM logs
WHERE visited::date >= CURRENT_DATE - INTERVAL '30 day';

-- 上月活跃玩家数（用于计算增长率）
SELECT COUNT(DISTINCT user_name)::int AS count
FROM logs
WHERE visited::date >= CURRENT_DATE - INTERVAL '60 day'
  AND visited::date < CURRENT_DATE - INTERVAL '30 day';

-- 总缓存数
SELECT COUNT(*)::int AS count FROM caches;

-- 已发现缓存覆盖
SELECT COUNT(DISTINCT gc_code)::int AS total_found FROM logs;
```

---

## 趋势计算逻辑

对于所有时间范围的排行榜，都需要计算 `trend` 和 `trendDelta`（基于**排名变化**）：

### 算法说明

```python
def calculate_trend(current_rank, previous_rank):
    """
    current_rank: 当前周期的排名 (1-based)
    previous_rank: 上一周期的排名 (1-based), 0 表示未上榜
    
    返回: (trend, trendDelta)
    """
    if previous_rank == 0:
        # 新上榜玩家
        return ("up", current_rank)
    
    delta = previous_rank - current_rank
    
    if delta > 0:
        # 排名数字减小 = 排名上升 (例如: 4->2)
        return ("up", abs(delta))
    elif delta < 0:
        # 排名数字增大 = 排名下降 (例如: 2->4)
        return ("down", abs(delta))
    else:
        return ("flat", 0)
```

### 上一个周期定义

| 当前周期 | 上一个周期 |
|---------|-----------|
| **30d** (最近30天) | 前 30-60 天 |
| **ytd** (今年至今) | 去年全年（1月1日 至 12月31日）|
| **all** (所有时间) | 截至去年年底的全部时间（12月31日及之前）|

### 示例

| 玩家 | 当前排名 | 上期排名 | trend | trendDelta | 说明 |
|------|---------|---------|-------|------------|------|
| A | 2 | 4 | `up` | 2 | 排名提升2位 |
| B | 5 | 3 | `down` | 2 | 排名下降2位 |
| C | 10 | 10 | `flat` | 0 | 排名不变 |
| D | 1 | 0 | `up` | 1 | 新上榜 |

---

## 数据验证规则

生成的 JSON 文件必须满足以下验证规则：

### overview.json
- [ ] `generatedAt` 是有效的 ISO 8601 格式时间戳
- [ ] `regions` 数组长度为 4，且 key 顺序正确
- [ ] 每个 `regionScopes[key]` 都存在且不为空
- [ ] `summary.metrics` 的4个指标都是非负整数
- [ ] `yearlyTrend` 至少包含1年的数据
- [ ] `dtMatrix` 包含恰好 81 个元素
- [ ] 所有 percentage 字段在 0-100 范围内

### player-rankings.json
- [ ] `rankings` 包含 4 种类型 × 3 种时间范围 = 12 个数组
- [ ] 每个数组长度不超过 30
- [ ] 每个条目的 rank 从 1 开始连续递增
- [ ] 所有 score 为非负整数
- [ ] trend 只能是 "up"、"down"、"flat" 之一
- [ ] `communityStats.activePlayersGrowthPct` 可以是负数（表示下降）
- [ ] `communityStats.foundCacheCoveragePct` 在 0-100 范围内

### city-rankings.json
- [ ] `rankings` 包含 4 种类型 × 3 种时间范围 = 12 个数组
- [ ] 每个数组长度不超过 20
- [ ] 每个条目的 rank 从 1 开始连续递增
- [ ] 所有 score 为非负整数
- [ ] `cacheTrend.bars` 包含恰好 7 年的数据
- [ ] `dtMatrix` 包含恰好 81 个元素

---

## 错误处理建议

1. **数据库连接失败**: 记录错误日志，不生成或保持上次成功的文件不变
2. **查询超时**: 设置合理的超时时间（建议 30 秒），超时则跳过该数据文件
3. **部分数据失败**: 允许部分文件生成成功，标记失败的文件
4. **空数据处理**: 
   - 排行榜为空时返回空数组 `[]`
   - metrics 为 0 时返回 0，不要返回 null
5. **数据一致性**: 所有文件应该在同一事务或同一时间点生成，避免跨文件数据不一致

---

## 性能优化建议

1. **批量查询**: 将多个独立的 SQL 查询合并为一次数据库连接中的多次查询
2. **并行执行**: 不同数据文件的生成可以并行进行
3. **缓存利用**: 对于重复使用的子查询结果，考虑使用 CTE 或临时表
4. **索引检查**: 确保 `placed_date`、`visited`、`owner_username`、`user_name`、`country`、`city` 字段有适当的索引

---

## 前端读取示例

前端可以通过 fetch 直接读取这些静态 JSON 文件：

```typescript
// 替代原来的 API 调用
const response = await fetch('/data/overview.json');
const data = await response.json();
```

或者为了更好的缓存控制，可以在 URL 中添加版本号或时间戳：

```typescript
const response = await fetch(`/data/overview.json?t=${Date.now()}`);
```

---

## 下一步行动项

1. 在另一个目录创建 Node.js / Python 脚本
2. 使用上述 SQL 查询从 Neon PostgreSQL 提取数据
3. 按照 JSON 结构格式化输出到 `public/data/` 目录
4. 配置 GitHub Actions 定时触发（建议每天 UTC 时间 00:00 执行）
5. 生成的文件提交到 Git 仓库并部署

---

**文档版本**: 1.2.0
**最后更新**: 2026-04-14
**适用项目**: Geodataing - 中国地理藏宝数据分析平台

---

## 更新日志

### v1.2.0 (2026-04-14)

**新增功能：并列排名与城市趋势支持**

1. **并列排名规则**
   - 分数相同的条目显示相同排名序号
   - 下一组跳过被占用的序号
   - 超限显示：并列导致超过限制时全部显示

2. **城市排行榜增强**
   - 新增 `trend` 和 `trendDelta` 字段
   - 与玩家排行榜使用相同的趋势计算逻辑

3. **数据过滤**
   - 不再显示分数为0的记录

4. **前端渲染优化**
   - 金银铜颜色规则明确
   - 排行榜三栏居中对齐

### v1.1.0 (2026-04-14)

**重大变更：趋势计算逻辑重构**

1. **趋势计算基础变更**
   - 从基于**分数变化**改为基于**排名变化**
   - `trendDelta` 现在表示排名的绝对变化值
   - 新增"新上榜"场景的处理逻辑

2. **上一周期定义更新**
   - `ytd`: 从"去年同期"改为"去年全年（1月1日-12月31日）"
   - `all`: 从"无（固定flat）"改为"截至去年年底的全部时间"

3. **性能优化**
   - 趋势计算查询从 N+1 次优化为 2 次（每个时间范围组合）
   - 查询次数减少约 96%
