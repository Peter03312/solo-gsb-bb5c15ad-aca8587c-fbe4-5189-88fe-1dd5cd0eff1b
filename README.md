# 园区访客通行规划服务（Park Route Planner）

面向园区访客的**离线**通行规划 HTTP 服务。导入一张带方向、通行秒数、台阶标记的
步道图，以及按**半开时间区间** `[start, end)` 生效的封闭记录；给定起终点、出发
时刻与是否避台阶，返回**最早到达**路线及逐段的进入、离开时间。查询还可以指定
**按顺序停靠的地点**（每站含可开始停留的时间窗与非负停留秒数），服务据此计算
各站抵达/开始/结束时刻并规划全程。

- 语言/存储：Python 3.11 + SQLite（标准库，无需外部数据库）
- Web 框架：FastAPI + Uvicorn
- 无外部网络依赖，适合园区内网 / 边缘设备离线运行

---

## 1. 快速启动

### 方式 A：Docker Compose（推荐，一键启动）

```bash
docker compose up -d --build
# 查看状态
curl http://localhost:8080/health
```

容器启动时会自动建库并导入内置示例数据（`app/sample_data.py`），随后在
`8080` 端口提供服务。数据持久化在命名卷 `park-route-data` 中。

停止：

```bash
docker compose down            # 保留数据
docker compose down -v         # 连同数据卷一起删除
```

### 方式 B：本地 Python 运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 建库 + 初始化数据（默认内置示例；--graph/--closures 可指定自定义 JSON）
python -m app.cli init-db

# 启动服务
python -m app.cli serve
# 或：uvicorn app.main:app --host 0.0.0.0 --port 8080
```

> 直接用 `uvicorn` 启动时若库为空，服务可正常运行，只是查询会返回 `503 no_data`，
> 请改用导入接口写入数据，或使用 `python -m app.cli serve` / Docker 入口（会自动种子化）。

**服务地址与端口**：默认 `http://0.0.0.0:8080`，可用 `APP_HOST` / `APP_PORT` 修改。
交互式 API 文档：`http://localhost:8080/docs`。

---

## 2. 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `APP_PORT` | `8080` | HTTP 监听端口 |
| `APP_DB_PATH` | `./data/routing.db`（容器内 `/data/routing.db`） | SQLite 数据库文件路径 |
| `APP_SEED_GRAPH` | （空） | 首次初始化使用的步道图 JSON 文件路径 |
| `APP_SEED_CLOSURES` | （空） | 首次初始化使用的封闭记录 JSON 文件路径 |
| `APP_SEED_SAMPLE` | `1` | 未指定种子文件时，是否导入内置示例数据；`0`/`false` 表示空库启动 |

---

## 3. 数据模型

### 步道图

- **节点**：字符串标识，如 `"GATE"`、`"PLAZA"`。
- **路段（边）**：有方向；字段：
  - `id`：路段编号，**同一版本内唯一**，用于平局字典序比较；
  - `from` / `to`：起终点，必须引用已声明的节点；
  - `travel_seconds`：通行秒数，**正整数**；
  - `has_stairs`：是否含台阶（布尔）。

示例见 [`data/graph.example.json`](data/graph.example.json)。

### 封闭记录

- 作用于某条边，时间区间为**半开** `[start, end)`：
  - `end` 时刻该路段**立即恢复**，允许恰好在 `end` 进入；
  - `start` 时刻该路段已经封闭。
- 必须满足 `start < end`，时间使用 ISO 8601（推荐 `...Z`）；不带时区按 UTC 处理。

示例见 [`data/closures.example.json`](data/closures.example.json)。封闭记录为**整体替换**。

### 通行与等候规则

- 允许在节点等候封闭结束；
- 一条边的整个通行区间 `[enter, enter + travel_seconds)` 不得与该边任何封闭区间重叠，
  不允许“先走进路段、在路中间等”；
- 寻路时自动计算每段实际进入时刻与等候秒数。

### 按顺序停靠点（stops）

`POST /api/route` 支持可选字段 `stops`：访客**按数组顺序**依次停靠的地点。
每站包含：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `node` | 字符串 | 停靠节点，必须是图中已声明节点 |
| `earliest_start` | ISO 8601 时间或非负纪元秒 | 最早开始停留时刻（**封闭端点，含**） |
| `latest_start` | ISO 8601 时间或非负纪元秒 | 最晚开始停留时刻（**封闭端点，含**） |
| `dwell_seconds` | 非负整数 | 停留秒数，允许 `0`（时刻打卡式停靠） |

规则：

- 抵达停靠节点后可在节点等候窗口开放；开始停留时刻 `start` 满足
  `earliest_start <= start <= latest_start`，即时间窗为**封闭区间**，
  恰好在窗口两端开始都合法；必须满足 `earliest_start <= latest_start`；
- 停留结束时刻为 `start + dwell_seconds`，**结束后才能**前往下一站；
- 站点必须严格按顺序完成：途中经过**尚未轮到**的站点不算完成停靠；
- 任一站无法在最晚时刻前开始停留（或该站/终点不可达），返回 `404 no_route`；
  时间窗无效、停留秒数为负/非整数、停靠地点未知等返回 `400 invalid_payload`；
- 选路目标不变：先比**最终到达终点**的时刻，平局再比**全程**路段编号序列的
  字典序——不逐段独立打破平局（窗口等候可能吸收前段耗时差异）；
- 无 `stops` 字段或为空数组时，请求与响应结构与旧版完全一致。

---

## 4. HTTP API

所有请求/响应均为 JSON。

### `POST /api/route` — 查询路线

请求：

```json
{
  "origin": "GATE",
  "destination": "TOWER",
  "departure_time": "2026-10-05T09:55:00Z",
  "avoid_stairs": false,
  "stops": [
    {
      "node": "PLAZA",
      "earliest_start": "2026-10-05T10:30:00Z",
      "latest_start": "2026-10-05T11:00:00Z",
      "dwell_seconds": 600
    }
  ]
}
```

`stops` 可省略或为 `[]`（无停靠点，沿用旧结构）。响应在原有字段基础上，
仅当存在停靠点时增加 `stops` 数组，逐站给出**抵达、开始、结束**时刻：

```json
{
  "arrival_time": "2026-10-05T10:49:00Z",
  "edge_sequence": ["E1", "E3", "E5"],
  "segments": [ ... 全程逐段时间（enter/leave/waiting）... ],
  "stops": [
    {
      "index": 0,
      "node": "PLAZA",
      "arrival_time": "2026-10-05T09:58:00Z",
      "start_time": "2026-10-05T10:30:00Z",
      "end_time": "2026-10-05T10:40:00Z",
      "earliest_start": "2026-10-05T10:30:00Z",
      "latest_start": "2026-10-05T11:00:00Z",
      "dwell_seconds": 600,
      "waiting_seconds": 1920
    }
  ],
  "total_waiting_seconds": 1920,
  "data_version": 2
}
```

- `stops[i]` 的 `arrival_time` 为抵达该站的时刻，`start_time` 为实际开始
  停留时刻（窗口未开放时晚于抵达），`end_time = start_time + dwell_seconds`，
  `waiting_seconds` 为抵达后等候窗口开放的秒数；
- `segments` 仍给出**全程逐段**的进入/离开时刻；停靠后第一段的
  `enter_time` 等于上一站 `end_time`（若再遇封闭则继续在节点等候）；
- `total_waiting_seconds` 只统计路段进入前的等待（封闭 + 窗口等待），
  不含 `dwell_seconds` 停留时间。

无停靠点时请求与旧版一致：

```json
{
  "origin": "GATE",
  "destination": "TOWER",
  "departure_time": "2026-10-05T09:55:00Z",
  "avoid_stairs": false
}
```

无停靠点时响应也不含 `stops` 字段（节选）：

```json
{
  "origin": "GATE",
  "destination": "TOWER",
  "query_departure_time": "2026-10-05T09:55:00Z",
  "arrival_time": "2026-10-05T10:07:00Z",
  "total_waiting_seconds": 0,
  "avoid_stairs": false,
  "edge_sequence": ["E1", "E3", "E5"],
  "segments": [
    {
      "edge_id": "E1",
      "from_node": "GATE",
      "to_node": "PLAZA",
      "has_stairs": false,
      "enter_time": "2026-10-05T09:55:00Z",
      "leave_time": "2026-10-05T09:58:00Z",
      "travel_seconds": 180,
      "waiting_seconds": 0
    }
  ],
  "data_version": 2
}
```

- 每段都给出**进入**（`enter_time`，已含节点等候）与**离开**（`leave_time`）时刻，
  以及 `waiting_seconds`；
- `data_version` 标明本次查询实际使用的数据版本。

错误：

| 场景 | 状态码 | `error` |
| --- | --- | --- |
| 起终点/停靠点未知、时间格式错误、时间窗无效、停留秒数非法等 | `400` | `invalid_payload` |
| 无可行路线（含避台阶后不可达、任一站无法按时停留、终点不可达） | `404` | `no_route` |
| 尚未导入步道图 | `503` | `no_data` |

### `POST /admin/graph` — 原子发布新步道图

```bash
curl -X POST http://localhost:8080/admin/graph \
  -H 'Content-Type: application/json' \
  -d @data/graph.example.json
```

校验失败（未知节点引用、非正通行秒数、编号重复等）返回 `400`，响应体含
`"rejected": true` 与当前仍生效的 `active_version`，**旧版本完整保留**。
发布成功后旧封闭记录中引用了已删除路段的条目会自动清理。

### `POST /admin/closures` — 原子整体替换封闭记录

```bash
curl -X POST http://localhost:8080/admin/closures \
  -H 'Content-Type: application/json' \
  -d @data/closures.example.json
```

引用不存在的路段、`start >= end` 等返回 `400` 并保留旧版本。

### 其他

- `GET /health`：健康检查与当前版本号；
- `GET /version`：当前版本及节点/路段/封闭数量。

---

## 5. 调用示例

```bash
# 09:55 出发（E4 在 10:00–11:00 封闭），不避台阶 → 绕行平路
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T09:55:00Z","avoid_stairs":false}'

# 避台阶：只经过 has_stairs=false 的路段
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T08:00:00Z","avoid_stairs":true}'

# 节点等候：HILL 09:59 出发，E4 [10:00,11:00) 封 → 在 HILL 等到 11:00 进入
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"HILL","destination":"TOWER",
       "departure_time":"2026-10-05T09:59:00Z","avoid_stairs":false}'

# 按顺序停靠：09:55 出发，PLAZA 10:30–11:00 之间开始停留 10 分钟
# → 09:58 到 PLAZA 等窗口，10:30 开始、10:40 结束，随后走 E3/E5，10:49 到 TOWER
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T09:55:00Z","avoid_stairs":false,
       "stops":[{"node":"PLAZA",
                 "earliest_start":"2026-10-05T10:30:00Z",
                 "latest_start":"2026-10-05T11:00:00Z",
                 "dwell_seconds":600}]}'

# 多停靠点：先 PLAZA（停留 1 分钟）再 LAKE（停留 2 分钟）
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T08:00:00Z","avoid_stairs":false,
       "stops":[{"node":"PLAZA",
                 "earliest_start":"2026-10-05T09:00:00Z",
                 "latest_start":"2026-10-05T09:30:00Z","dwell_seconds":60},
                {"node":"LAKE",
                 "earliest_start":"2026-10-05T10:00:00Z",
                 "latest_start":"2026-10-05T12:00:00Z","dwell_seconds":120}]}'

# 无法按时停靠（最早抵达 TOWER 晚于最晚开始时刻）→ 404 no_route
curl -s -i -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T09:55:00Z",
       "stops":[{"node":"TOWER",
                 "earliest_start":"2026-10-05T08:00:00Z",
                 "latest_start":"2026-10-05T08:01:00Z","dwell_seconds":0}]}'

# 冒烟自检（需服务已启动）
python3 scripts/smoke_test.py http://localhost:8080
```

Python 示例：

```python
import json, urllib.request

req = urllib.request.Request(
    "http://localhost:8080/api/route",
    data=json.dumps({
        "origin": "GATE",
        "destination": "TOWER",
        "departure_time": "2026-10-05T09:55:00Z",
        "avoid_stairs": True,
    }).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as resp:
    route = json.load(resp)
print(route["arrival_time"], route["edge_sequence"])
```

---

## 6. 选路与版本语义

- **选路目标**：① 最终到达终点最早；② 到达时刻相同时，按**全程路段编号序列
  字典序**选路（字符串逐元素比较，例如 `("E1","E3") < ("E2","E4")`）。
  停靠点的时间窗等候可能把不同前段路径的出发时刻拉平，因此平局比较始终针对
  全程序列，**不逐段独立打破平局**。
- **算法**：
  1. 时间依赖的 Dijkstra（FIFO 网络）按停靠顺序逐路段计算最早到达，抵达站点后
     在窗口内取最早可开始时刻，停留结束作为下一路段出发时刻，得到终点最早到达 `T`；
  2. 在“到达终点不晚于 `T`”的分阶段状态空间（节点 + 已完成停靠数）做多标签搜索，
     停靠点产生零路段的“停留转移”，取 `(到达时刻, 全程序列)` 最优者。第二阶段用于
     处理“节点/窗口等候把不同路径的到达时刻拉平”后，单标号算法可能选错字典序
     路径的问题。
- **原子发布与版本一致**：
  - 每次导入（图或封闭）在一个 SQLite 写事务中完成全部校验、复制与版本指针切换，
    失败整体回滚；
  - 每次查询在开始时读取一次 `current_version` 并固定，**整次请求只用同一版本**，
    导入进行中或刚完成都不会看到半个版本；
  - 历史版本在库中保留，可按版本号追溯。

---

## 7. 项目结构

```
app/
  config.py        # 环境变量配置
  timeutil.py      # ISO 8601 <-> 纪元秒（UTC）
  schemas.py       # 导入/查询负载校验
  db.py            # SQLite 版本化与原子发布
  routing.py       # 时间依赖最短路 + 字典序选路引擎
  main.py          # FastAPI 路由
  sample_data.py   # 内置示例数据
  cli.py           # init-db / import-* / serve
data/              # 示例 JSON 与本地数据库
scripts/smoke_test.py
tests/             # 标准库 unittest 测试（65 个用例）
Dockerfile
docker-compose.yml
docker-entrypoint.sh
```

### 运行测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖半开区间边界、节点等候、封闭穿越判定、方向、避台阶、字典序平局、
按顺序停靠（封闭时间窗边界、零停留、窗口等待、站点顺序、跨停靠封闭等待、
全程序列平局不逐段打破）、无路线、原子发布回滚与版本快照等场景。
