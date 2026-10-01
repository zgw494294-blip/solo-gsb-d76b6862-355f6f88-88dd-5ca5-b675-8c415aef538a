# 离线对白字幕分轨 HTTP API

用 Python + FastAPI 实现的离线服务：给定 2～3 条屏幕轨道和 1～12 条对白字幕，为每条字幕选择一条轨道，满足：

- **轨道互斥**：字幕占用左闭右开时间区间 `[start_ms, end_ms)`；时间重叠的字幕不能占同一轨；端点相接（前一条的 `end_ms` == 后一条的 `start_ms`）可以复用同一轨。
- **锁定**：字幕可指定 `locked_track`，强制占用该轨道（轨道号从 1 开始）。
- **优化目标**：同一说话人按时间相邻的两条字幕，换轨次数总和最小。
- **并列裁决**：换轨次数相同时，取“按输入顺序排列的轨道号序列”字典序最小的方案。
- 同一说话人的字幕时间不得重叠（属于输入合法性约束，非法返回 422）。
- 分轨结果可作为**场景（scene）**按 `scene_id` 保存为版本化修订：新场景以 `expected_revision: 0` 创建（成功后版本 1），后续修订须带当前版本；版本不符返回 409。数据落在本地 SQLite，容器重启后仍可读取。
- 已保存场景可按 `scene_id` 建立**单进程内存播放会话**：复制完整快照、游标从 0 毫秒开始，推进只能不减；播放中可**接纳**场景后续修订——已开始字幕的身份与分轨固定，其余字幕重算，冲突或无解返回 409。会话不跨重启保留。

## 接口一览

| 方法 | 地址 | 说明 |
| --- | --- | --- |
| POST | `http://localhost:8080/assign` | 一次性分轨计算，不保存 |
| PUT | `http://localhost:8080/scenes/{scene_id}` | 创建/修订场景（乐观锁版本控制） |
| GET | `http://localhost:8080/scenes/{scene_id}` | 读取场景最近快照 |
| POST | `http://localhost:8080/scenes/{scene_id}/session` | 创建（或重置）内存播放会话 |
| POST | `http://localhost:8080/scenes/{scene_id}/session/advance` | 推进游标（非负、不减） |
| POST | `http://localhost:8080/scenes/{scene_id}/session/adopt` | 接纳场景当前快照 |
| GET | `http://localhost:8080/scenes/{scene_id}/session` | 查看会话完整状态 |
| GET | `http://localhost:8080/health` | 健康检查 |
| GET | `http://localhost:8080/docs` | Swagger 交互文档 |

## 一条命令启动

```bash
docker compose up --build -d
```

服务监听 **8080** 端口，接口地址：`http://localhost:8080/assign`

停止：`docker compose down`

> - 交互式查看日志可去掉 `-d`。交互式文档：`http://localhost:8080/docs`（Swagger UI）。
> - Compose 自动把具名卷 `scenes-data` 挂载到容器内 `/data`：场景数据库（`SCENES_DB_PATH=/data/scenes.db`）在容器重启甚至 `docker compose down && up` 后仍然保留。仅 `docker compose down -v` 删除卷时才会清除场景。
> - 播放会话是**单进程内存态**：Compose/Dockerfile 默认只启动一个 uvicorn 进程，推进与接纳在进程内串行生效；多进程（多 worker/多机）部署不共享会话，需要时请自行保证单实例。

## 请求

`POST /assign`，JSON Body：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `track_count` | int | 屏幕轨道数，2 或 3 |
| `subtitles` | array | 1～12 条，顺序即输入顺序 |
| `subtitles[].id` | string \| int | 全局唯一，字符串不得为空 |
| `subtitles[].speaker` | string | 非空（不能全为空白） |
| `subtitles[].start_ms` | int | 非负 |
| `subtitles[].end_ms` | int | 非负，且严格大于 `start_ms` |
| `subtitles[].locked_track` | int \| null | 可选，锁定轨道号（≥1 且 ≤ `track_count`） |

## 响应

- 有解：HTTP 200，`feasible: true`，`changes` 为换轨总数，`assignments` 按**输入原顺序**给出每条字幕的轨道。
- 锁定冲突或轨道不足：HTTP 200，`feasible: false`，`changes: 0`，`assignments: []`（空方案）。
- 非法输入（字段缺失/越界、id 重复、同一说话人字幕时间重叠等）：HTTP **422**，带校验错误明细。

## 可执行的 HTTP 调用示例

### 示例 1：有解（含锁定轨道与端点相接）

```bash
curl -s http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
      {"id": "s4", "speaker": "Alice", "start_ms": 2500, "end_ms": 4000}
    ]
  }'
```

响应：

```json
{
  "feasible": true,
  "changes": 0,
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1},
    {"id": "s4", "track": 1}
  ]
}
```

说明：s1 与 s3 时间相接可同轨，s3 与 s4 同轨；Alice 三条字幕在同一轨，换轨 0 次。

### 示例 2：非零换轨（锁定迫使相邻字幕分轨）

```bash
curl -s http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "a", "speaker": "A", "start_ms": 0,   "end_ms": 100, "locked_track": 1},
      {"id": "b", "speaker": "A", "start_ms": 100, "end_ms": 200, "locked_track": 2},
      {"id": "c", "speaker": "A", "start_ms": 200, "end_ms": 300}
    ]
  }'
```

响应：

```json
{
  "feasible": true,
  "changes": 1,
  "assignments": [
    {"id": "a", "track": 1},
    {"id": "b", "track": 2},
    {"id": "c", "track": 2}
  ]
}
```

### 示例 3：无解 —— 两条重叠字幕锁定同一轨（空方案）

```bash
curl -s http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 2000, "locked_track": 1},
      {"id": "s2", "speaker": "Bob",   "start_ms": 1000, "end_ms": 3000, "locked_track": 1}
    ]
  }'
```

响应：

```json
{"feasible": false, "changes": 0, "assignments": []}
```

### 示例 4：无解 —— 3 条字幕同时重叠但只有 2 轨（轨道不足）

```bash
curl -s http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "a", "speaker": "A", "start_ms": 0,  "end_ms": 100},
      {"id": "b", "speaker": "B", "start_ms": 10, "end_ms": 110},
      {"id": "c", "speaker": "C", "start_ms": 20, "end_ms": 120}
    ]
  }'
```

响应：

```json
{"feasible": false, "changes": 0, "assignments": []}
```

### 示例 5：非法输入返回 422（同一说话人字幕时间重叠）

```bash
curl -s -i http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "a", "speaker": "A", "start_ms": 0,   "end_ms": 100},
      {"id": "b", "speaker": "A", "start_ms": 50,  "end_ms": 150}
    ]
  }'
```

返回 `HTTP/1.1 422 Unprocessable Entity`，body 为 FastAPI 标准校验错误结构。

## 场景（scene）：可保存的修订

`/assign` 只做一次性计算、不保存任何数据。如果需要把某次分轨结果保存下来、以后读取并在其基础上继续修订，使用场景接口，数据写入容器内本地 SQLite（Docker Compose 已挂载具名卷，**容器重启甚至重新创建后场景仍可读取**）。

- `PUT /scenes/{scene_id}`：创建或修订场景。请求体沿用 `/assign` 的 `track_count` 与 `subtitles`（同样的字段约束、校验、最少换轨与并列规则），另加一个字段：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `expected_revision` | int | 非负。客户端所依据的当前版本：**新场景填 `0`**；修订已存在场景时必须填写其当前版本 |

- 新场景首次成功后 `revision = 1`；此后每次成功修订 `revision + 1`。
- 合法但无解：返回与 `/assign` **完全相同**的无解响应（`feasible: false`、空方案），**不保存、不改变任何已有版本**。
- 非法输入：**422**，不改变已有版本。
- 版本不符（含同一版本的并发竞争落败方）：**409**，响应体给出 `current_revision`，不改变已有版本。同一版本的并发修订最多只有一次成功，读取结果永远是某次成功提交的完整快照，不会混用两次提交的数据。
- `GET /scenes/{scene_id}`：读取最近一次成功修订保存的完整场景（字幕、分轨、changes、版本）；场景不存在返回 **404**。
- 数据库路径可用环境变量 `SCENES_DB_PATH` 覆盖（Compose 中为 `/data/scenes.db`）。

### 示例 6：创建场景（`expected_revision=0`，成功后版本为 1）

```bash
curl -s -X PUT http://localhost:8080/scenes/scene-001 \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500}
    ]
  }'
```

响应（HTTP 200）：

```json
{
  "scene_id": "scene-001",
  "revision": 1,
  "track_count": 2,
  "subtitles": [
    {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000, "locked_track": null},
    {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
    {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500, "locked_track": null}
  ],
  "changes": 0,
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1}
  ],
  "feasible": true
}
```

### 示例 7：在当前版本上修订（`expected_revision` 填当前版本，成功后 +1）

```bash
curl -s -X PUT http://localhost:8080/scenes/scene-001 \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1200},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000},
      {"id": "s3", "speaker": "Alice", "start_ms": 1200, "end_ms": 2500}
    ]
  }'
```

响应中的 `"revision": 2`。再次修订时需把 `expected_revision` 改为 `2`。

### 示例 8：读取场景

```bash
curl -s http://localhost:8080/scenes/scene-001
```

返回最近一次成功修订的完整快照（结构同示例 7 的成功响应）。场景不存在时返回 `HTTP/1.1 404 Not Found`。

### 示例 9：版本冲突返回 409（不改动已有版本）

假设场景当前版本为 2，却仍按旧版本 1 提交：

```bash
curl -s -i -X PUT http://localhost:8080/scenes/scene-001 \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000}
    ]
  }'
```

```
HTTP/1.1 409 Conflict
```

```json
{
  "detail": {
    "message": "场景版本冲突：expected_revision 与当前版本不一致",
    "scene_id": "scene-001",
    "current_revision": 2
  }
}
```

此时读取场景，`revision` 仍为 `2`，字幕与分轨仍是修订前的内容。客户端应先 `GET /scenes/scene-001` 取得当前版本，再带上新的 `expected_revision` 重试。对一个已存在的场景提交 `expected_revision: 0`（按新场景创建）同样返回 409；同一版本的并发提交也只有一次成功，其余均返回此响应。

> 合法但无解的场景修订不产生 409/200 快照，而是与示例 3/4 的 `/assign` 一样返回 `{"feasible": false, "changes": 0, "assignments": []}`，且不落库、不推进版本。非法输入（缺少 `expected_revision`、`track_count` 越界、说话人字幕重叠等）返回 422，同样不改动已有版本。

## 播放会话（playback session）：播放中接纳后续修订

场景开始播放后，客户端可能继续提交修订。播放会话让“正在播放的快照”在接纳新修订时**保持已开始字幕的轨道不动**，只对游标之后的内容重新分轨。

- 会话是**单进程内存态**：按 `scene_id` 建立，复制已保存场景的**完整快照**（字幕、分轨、changes、版本），播放游标 `cursor_ms` 从 **0** 开始。
- 会话**无需跨重启保留**：服务进程重启后会话消失（已保存的场景不受影响），重新建会即可。一个 `scene_id` 同时只有一个会话，重复建会即按场景最新快照整体重置（游标归零）。
- 接口地址（均基于场景地址）：
  - `POST http://localhost:8080/scenes/{scene_id}/session`：建会（或重置）；
  - `POST http://localhost:8080/scenes/{scene_id}/session/advance`：推进游标；
  - `POST http://localhost:8080/scenes/{scene_id}/session/adopt`：接纳场景当前快照；
  - `GET  http://localhost:8080/scenes/{scene_id}/session`：查看会话完整状态。

### 推进：`POST .../session/advance`

请求体 `{"time_ms": <非负整数>}`：游标**只能不减**（相等允许，即原地停留）。

- 非法时间（负数、非整数、缺字段）或倒退：**422**，失败不改动会话。
- 成功返回源版本 `source_revision`、`cursor_ms`、游标处**活动字幕** `active` 与按轨道分组的 `tracks`。
- 活动字幕仍按**左闭右开** `[start_ms, end_ms)` 判定：`start_ms <= cursor_ms < end_ms`，端点相接不算重叠。

### 接纳：`POST .../session/adopt`（无请求体）

以该 `scene_id` **当前已保存**的场景快照替换会话快照，规则：

1. **轨道数不得改变**（与建会时一致），否则 409；
2. 会话中 `start_ms <= cursor_ms` 的字幕（已开始，含已经播完的）视为既成事实：其 `id`、`speaker`、`start_ms`、`end_ms`、`locked_track` **必须逐字不变**，并且**固定会话中的原分轨**，不得删除；
3. 其余字幕允许修改、删除或新增，但它们在新快照中的 `start_ms` 都必须**严格晚于游标**；
4. 以**新快照的顺序**，沿用原“最少换轨、轨道序列字典序最小”的并列规则，把已开始字幕的固定轨道作为锁定约束，对**全部**字幕重新求解分轨；
5. 接纳冲突（规则 1～3 任一不满足）或重算**无解**：返回 **409**（`detail.reason` 给出原因），**会话保持原样**；
6. 接纳成功：**整体替换**会话快照与 `source_revision`，游标不变，`changes`/`assignments` 为重算结果；**已保存的场景本身不被修改**。

推进与接纳的并发请求在服务端按**完整操作串行生效**，因此读到的永远是某个完整操作之后的自洽状态。

### 示例 10：建会并推进（游标左闭右开）

```bash
# 先用示例 6 的场景 scene-001（revision=1；s1=[0,1000) 轨1，s2=[800,3000) 轨2，s3=[1000,2500) 轨1）
curl -s -X POST http://localhost:8080/scenes/scene-001/session
curl -s -X POST http://localhost:8080/scenes/scene-001/session/advance \
  -H 'Content-Type: application/json' \
  -d '{"time_ms": 800}'
```

推进到 800ms 的响应：

```json
{
  "scene_id": "scene-001",
  "source_revision": 1,
  "cursor_ms": 800,
  "active": [
    {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000, "locked_track": null, "track": 1},
    {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2,    "track": 2}
  ],
  "tracks": [
    {"track": 1, "occupied": [{"id": "s1", "speaker": "Alice", "start_ms": 0,   "end_ms": 1000, "locked_track": null, "track": 1}]},
    {"track": 2, "occupied": [{"id": "s2", "speaker": "Bob",   "start_ms": 800, "end_ms": 3000, "locked_track": 2,    "track": 2}]}
  ]
}
```

游标推进到 `1000` 时，s1 因右端点开区间而结束、s3 端点相接开始：

```bash
curl -s -X POST http://localhost:8080/scenes/scene-001/session/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 1000}'
# active 变为 s2、s3
```

倒退返回 422 且会话不变：

```bash
curl -s -i -X POST http://localhost:8080/scenes/scene-001/session/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 999}'
# HTTP/1.1 422 Unprocessable Entity
# detail 中含当前 cursor_ms=1000 与被拒绝的 time_ms=999
```

### 示例 11：播放中修订场景并接纳（已开始字幕固定原轨）

播放停在 800ms 时（s1、s2 已开始），照常通过 `PUT /scenes/scene-001` 提交 revision 2：已开始字幕原样保留，只改游标之后的内容（例如缩短 s3、新增 s5）：

```bash
curl -s -X PUT http://localhost:8080/scenes/scene-001 \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,   "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800, "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000,"end_ms": 2000},
      {"id": "s5", "speaker": "Carol", "start_ms": 2000,"end_ms": 2600}
    ]
  }'

curl -s -X POST http://localhost:8080/scenes/scene-001/session/adopt
```

接纳响应（HTTP 200）中 `source_revision` 变为 `2`、`cursor_ms` 仍为 `800`；`assignments` 为全量重算结果，其中 s1、s2 固定在轨道 1、2，s3、s5 按最少换轨/字典序规则重新分轨。之后继续推进即按新快照播放：

```bash
curl -s -X POST http://localhost:8080/scenes/scene-001/session/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 1500}'
# active: s2(轨2)、s3（重算后的轨道）
```

### 示例 12：接纳冲突返回 409（会话与场景都不变）

已开始字幕被修改/删除、轨道数改变、或新字幕在游标处及之前开始，都会被拒绝。例如游标在 800ms，却把已开始的 s1 改短：

```bash
curl -s -i -X POST http://localhost:8080/scenes/scene-001/session/adopt
```

（假设当前保存的 revision 2 中 s1 的 `end_ms` 已被改动）

```
HTTP/1.1 409 Conflict
```

```json
{
  "detail": {
    "message": "接纳修订失败：已开始字幕 's1' 的 end_ms 不得在播放中修改",
    "scene_id": "scene-001",
    "reason": "started_subtitle_changed",
    "subtitle_id": "s1"
  }
}
```

此时 `GET /scenes/scene-001/session` 看到的仍是接纳前的快照（`source_revision` 不变、游标不变）。其他 `reason` 取值：`track_count_changed`、`started_subtitle_missing`、`subtitle_starts_before_cursor`、`duplicate_id`、`infeasible`（固定已开始字幕后重算无解）。

> 接纳读取的是场景**已保存**的最新版本；合法但无解的修订根本不会落库（见示例 3/4 的无解响应），因此不会被接纳。接纳操作只改会话，不推进场景版本；场景仍需通过 `PUT /scenes/{scene_id}` 修订。

健康检查：

```bash
curl -s http://localhost:8080/health
# {"status":"ok"}
```

## 本地开发与测试（不使用 Docker）

需 Python 3.11+：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8080
pytest
```

本地（非 Docker）运行时，场景数据库默认写入工作目录下的 `scenes.db`；可用环境变量改到别处，例如 `SCENES_DB_PATH=/tmp/scenes.db uvicorn app.main:app --port 8080`。

核心求解器（`app/solver.py`）无第三方依赖，也可直接运行其测试：

```bash
python tests/test_solver.py
```

## 算法说明

输入规模很小（≤12 条、≤3 轨），采用带剪枝的深度优先枚举：

1. 按输入顺序逐条分配轨道，每个位置按轨道号 1→k 升序尝试；
2. 候选轨道必须未被任何时间重叠的已分配字幕占用，锁定字幕只尝试锁定轨；
3. 代价在“同一说话人按时间排序后相邻”的字幕对两端都落位时累计（换轨计 1）；
4. 剪枝：当前代价不低于已找到的最优解时剪枝。由于按轨道号升序枚举，第一个到达的最优解即为字典序最小的轨道序列，无需额外比较。

实现已用穷举参考解对 4000 组随机实例交叉验证。

## 目录结构

```
app/
  main.py      # FastAPI 路由与请求/响应模型（Pydantic 校验）
  solver.py    # 纯 Python 求解器与业务校验（无第三方依赖）
  storage.py   # 场景持久化：本地 SQLite（乐观锁版本控制）
  playback.py  # 单进程内存播放会话：游标推进、接纳修订（固定已开始字幕重算）
tests/
  conftest.py        # pytest 夹具（场景接口使用临时数据库、会话逐测试清空）
  test_solver.py     # 求解器单元测试
  test_api.py        # /assign HTTP 层测试
  test_scenes.py     # 场景创建/修订/读取/版本冲突/并发测试
  test_sessions.py   # 播放会话建会/推进/接纳/冲突/并发测试
Dockerfile
docker-compose.yml   # 挂载 scenes-data 具名卷到 /data
requirements.txt / requirements-dev.txt
```
