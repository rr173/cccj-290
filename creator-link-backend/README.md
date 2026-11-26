# 创作工作台账号关联后端

Python + FastAPI + SQLite 的本地演示/可运行服务。账号使用服务端生成的不透明 Bearer 会话凭据，不接外部登录提供商。

核心能力：

- 本地注册/登录，会话落库，**重启后仍然有效**。
- 项目只有 `title` 和 `content`；创建归属 `owner_id` 永远取创建请求的会话账号，且没有任何接口可以修改它。
- 一条有期限邀请必须由**两个不同账号分别显式确认**；单次完成只创建一条无向关联边。
- 关联图上的连通分量共享项目：直接边和链式传递都生效。
- 删除一条边后，下一次请求立即按新图鉴权；若仍有替代路径，访问不丢失。
- 项目修改历史只记录实际 Bearer 会话账号，并保留当时的 `owner_id` 与图版本，可区分“谁通过共享改了谁的项目”。
- 邀请确认、重复确认、并发删边、项目写入与解除关联竞争都由 SQLite 事务和唯一约束保护。
- **限量刊期**：所有者把某条已有修订发布为不可变快照并设总名额，经渠道树划拨、发邀、领取；名额、到期、停用在并发与重启后一致。
- 提供双会话脚本、三账号链路脚本、限量刊期脚本，以及真实 uvicorn 重启 + HTTP 并发校验脚本。

---

## 1. 运行

```bash
cd creator-link-backend
python3 -m pip install -r requirements.txt

# 可选：自定义数据库位置。默认是 ./data/workbench.db
export WORKBENCH_DB="$PWD/data/workbench.db"

python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

如需清空本地数据，停止服务后删除 `$WORKBENCH_DB` 以及同名 `-wal`、`-shm` 文件即可。

打开：

- Swagger UI：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health>

所有受保护接口使用：

```http
Authorization: Bearer <session_token>
```

也支持 `X-Session-Token: <session_token>`。

### 一键脚本

另开终端：

```bash
cd creator-link-backend

# 双账号/双会话：邀请、重复确认、共享写入、归属、历史、解除后立即 403
python3 scripts/demo_two_sessions.py http://127.0.0.1:8000

# 三账号 A-B-C：链式访问、断边分量、替代路径
python3 scripts/demo_three_chain.py http://127.0.0.1:8000

# 限量刊期：快照、划拨、邀请、领取、读者无项目权限、停用渠道
python3 scripts/demo_issues.py http://127.0.0.1:8000
```

或者让脚本自己启动临时服务并运行演示 + 重启/并发校验：

```bash
bash scripts/run_all_checks.sh
```

单独运行重启与并发一致性验证：

```bash
# 账号关联图：共享写入与删边竞争、离线图/历史不变式
python3 scripts/verify_restart_concurrency.py

# 限量刊期：并发划拨/发邀/领取、真实 uvicorn 重启、到期惰性结算
python3 scripts/verify_issues_restart_concurrency.py
```

运行单元/API 测试：

```bash
python3 -m pytest -q
```

---

## 2. HTTP API 速览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/register` | 本地注册并返回会话 token；`password` 可选 |
| POST | `/auth/login` | 用户名/密码登录，返回新的会话 token |
| GET | `/me` | 检查当前会话 |
| POST | `/auth/logout` | 撤销当前 token；不影响其他会话 |
| POST | `/invitations` | 创建有期限邀请，可指定 `invitee_username` |
| GET | `/invitations/{token}` | 查看邀请状态和已确认账号 |
| POST | `/invitations/{token}/confirm` | 当前账号显式确认同一条邀请 |
| GET | `/edges` | 列出与当前账号相连的有效无向边 |
| DELETE | `/edges/{edge_id}` | 删除一条关联边；并发/重复删除是幂等删除标记 |
| POST | `/projects` | 创建项目，owner 强制为当前会话账号 |
| GET | `/projects` | 列出当前连通分量内所有账号拥有的项目 |
| GET | `/projects/{id}` | 读取单个项目；拿到 ID 也必须通过图鉴权 |
| PATCH | `/projects/{id}` | 修改标题/内容；可选用整数 `If-Match` 做乐观锁 |
| GET | `/projects/{id}/history` | 查看只增历史、实际操作者、owner、图版本 |
| POST | `/projects/{id}/issues` | 所有者把某条已有修订发布为限量刊期（快照 + 总名额） |
| GET | `/projects/{id}/issues` | 所有者列出该项目所有刊期及名额概况 |
| GET | `/issues/{id}` | 所有者/管理人查看刊期信息与名额概况 |
| GET | `/issues/{id}/channels` | 所有者看全部渠道及余量；管理人只看自己管理的渠道 |
| POST | `/issues/{id}/channels` | 把可用名额划给指定管理账号，生成新下级渠道 |
| POST | `/issues/{id}/channels/{cid}/disable` | 停用渠道（所有者任意；管理人仅直接下级），覆盖整条下游 |
| POST | `/issues/{id}/invitations` | 从自己有权限的渠道向读者发邀请（占一个名额） |
| POST | `/issues/{id}/channels/{cid}/invitations` | 指定渠道发邀请 |
| GET | `/issues/{id}/channels/{cid}/invitations` | 渠道管理人/所有者列出该渠道邀请 |
| GET | `/me/issue-invitations` | 读者查看自己的邀请与已领权益 |
| POST | `/issues/invitations/{id}/accept` | 被邀请读者领取；重复接受返回原结果 |
| GET | `/me/issues` | 读者列出自己已领刊期 |
| GET | `/issues/{id}/read` | 读者读取已领刊期（发行时的标题与正文） |

---

## 3. 典型调用流程

### 3.1 注册两个本地会话

```bash
curl -sS -X POST http://127.0.0.1:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"demo-pass-123"}'
```

响应：

```json
{
  "account": {"id": 1, "username": "alice"},
  "session": {
    "token": "本地生成的随机 Bearer token",
    "token_type": "Bearer",
    "expires_at": "2026-..."
  }
}
```

Bob 同样注册。下面用 `$A_TOKEN`、`$B_TOKEN` 表示两个会话。

### 3.2 两个账号分别确认同一条邀请

Alice 创建邀请，可指定 Bob，也可以不指定而把随机 token 通过本地/带外方式发给第二个账号：

```bash
curl -sS -X POST http://127.0.0.1:8000/invitations \
  -H "Authorization: Bearer $A_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"invitee_username":"bob","ttl_seconds":3600}'
```

返回中的 `token` 是 256-bit 随机邀请凭据，数据库只存 SHA-256 哈希。Alice 创建邀请不会替自己或 Bob 完成同意，Alice 仍需确认：

```bash
curl -sS -X POST http://127.0.0.1:8000/invitations/$INVITE_TOKEN/confirm \
  -H "Authorization: Bearer $A_TOKEN"
# status = pending, confirmation_count = 1
```

Bob 必须用自己的会话确认同一条 token：

```bash
curl -sS -X POST http://127.0.0.1:8000/invitations/$INVITE_TOKEN/confirm \
  -H "Authorization: Bearer $B_TOKEN"
# status = consumed, edge = {...}
```

保证：

- `UNIQUE(invitation_id, account_id)`：同一账号重试不会消耗两次确认。
- 邀请完成事务中恰好读取到两个不同账号后，才创建一条边并把邀请标记为 `consumed`。
- `association_edges(account_a, account_b) WHERE deleted_version IS NULL` 局部唯一索引防止同一对账号出现两条有效边。
- 已消费邀请再次确认返回 `409 INVITATION_CONSUMED`；过期返回 `410 INVITATION_EXPIRED`，都不会再建边。

### 3.3 共享项目与归属

Alice 创建项目：

```bash
curl -sS -X POST http://127.0.0.1:8000/projects \
  -H "Authorization: Bearer $A_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"Plan","content":"v1"}'
```

关联后 Bob 可以读取和修改：

```bash
curl -sS http://127.0.0.1:8000/projects/$PROJECT_ID \
  -H "Authorization: Bearer $B_TOKEN"

curl -sS -X PATCH http://127.0.0.1:8000/projects/$PROJECT_ID \
  -H "Authorization: Bearer $B_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"content":"Bob edited through sharing"}'
```

响应中的 `owner_id` 仍然是 Alice。即使请求体额外带 `"owner_id": 999`，该未知字段也会被忽略；SQL 的 `UPDATE projects` 根本不更新 `owner_id`。

Bob 在关联期间创建的项目 owner 是 Bob；Alice 可以通过共享修改，但解除 A-B 后 owner 仍为 Bob。

### 3.4 历史区分实际操作者

```bash
curl -sS http://127.0.0.1:8000/projects/$PROJECT_ID/history \
  -H "Authorization: Bearer $A_TOKEN"
```

历史项示例：

```json
{
  "revision": 2,
  "action": "updated",
  "actor": {"id": 2, "username": "bob"},
  "owner": {"id": 1, "username": "alice"},
  "actor_is_owner": false,
  "shared_modification": true,
  "title": "Plan",
  "content": "Bob edited through sharing",
  "graph_version": 1
}
```

关联/解除关联不会修改这些行，因此事后仍能看出是 Bob 借共享关系修改了 Alice 的项目。

### 3.5 解除关联和即时失权

```bash
curl -sS http://127.0.0.1:8000/edges -H "Authorization: Bearer $A_TOKEN"
curl -sS -X DELETE http://127.0.0.1:8000/edges/$EDGE_ID \
  -H "Authorization: Bearer $A_TOKEN"
```

删除事务提交后，Bob 即使已经持有 `PROJECT_ID`，下一次请求也会立即得到：

```http
HTTP/1.1 403 Forbidden
```

```json
{
  "error": {
    "code": "PROJECT_ACCESS_DENIED",
    "message": "Current account is not connected to the project owner's component.",
    "details": {"project_id": 12}
  }
}
```

这不是仅从 `GET /projects` 列表隐藏：`GET /projects/{id}`、`PATCH /projects/{id}` 和历史接口都会重新计算当前连通分量。

重复 `DELETE /edges/{id}` 返回 `200` 和 `"idempotent_replay": true`，不会再次增加图版本或写入第二条 unlink 事件。

---

## 4. 链式关联与替代路径

当前有效边构成无向图。鉴权 SQL 使用递归 CTE：

```sql
WITH RECURSIVE reach(id) AS (
    SELECT :viewer_id
    UNION
    SELECT CASE WHEN e.account_a = reach.id
                THEN e.account_b ELSE e.account_a END
      FROM association_edges e
      JOIN reach ON reach.id IN (e.account_a, e.account_b)
     WHERE e.deleted_version IS NULL
)
SELECT ...
FROM projects p
WHERE p.owner_id IN (SELECT id FROM reach);
```

因此：

```text
Alice -- Bob -- Carol
```

Carol 可以访问 Alice 的项目，反之亦然。

删除 Alice-Bob 后：

```text
Alice        Bob -- Carol
```

Alice 与 Bob/Carol 的项目立即互不可访问；Bob-Carol 内部共享不变。

如果图是三角形：

```text
Alice -- Bob
   \     /
    Carol
```

删除 Alice-Bob 后仍有 `Alice-Carol-Bob` 路径，所以访问不变；再删除 Alice-Carol 才会把 Alice 分开。`scripts/demo_three_chain.py` 完整验证这些状态。

---

## 5. 限量刊期（Limited-edition issues）

工作台定稿可以发布为**限量刊期**交给合作渠道发行。刊期是一份**不可变快照**：发布时复制所选项目修订的 `title` 与 `content`，之后工作台继续改稿、改标题都不会影响已发布刊期（`issues` 表独立存储标题/正文，读取接口只读这份快照）。

### 5.1 名额与渠道树

- 每个刊期属于项目所有者，只有一个隐藏的**根渠道**，其 `available` 即所有者**未划拨**的名额。
- 所有者可把额度划给某个账号，每次划拨都**新建**一个渠道（同一账号划两次会得到两个渠道，旧渠道与其邀请不受影响）。
- 渠道管理人可继续把自己的可用额度划给下级账号，或从中发邀请。
- 划拨是**移动**：父渠道 `available` 立即减少，子渠道增加；同一份额度不会同时在两级可用。
- 核心不变式，在任何已提交状态都成立：

  ```text
  total_quota == Σ channels.available + 待领取邀请数 + 已领取权益数
  ```

### 5.2 邀请与领取

- 邀请**指定读者账号**并带**领取截止时间**，发出即从渠道扣 1 个名额（held）。
- 只有被指定的读者账号能领取；其他账号 `403 NOT_INVITED_READER`。
- 接受后转为永久**权益**（entitlement），可以一直读取该刊期；截止前未接受则邀请到期，名额释放。
- 同一读者在同一刊期**最多**一份未过期的待领邀请（无论来自哪个渠道）或一份已领权益。跨渠道重复邀请同一读者会复用原邀请，不占新名额、不延长截止时间。
- 重复接受返回原领取结果（`idempotent_replay: true`，同一 `entitlement`）。
- 读者只用自己的现有账号领取，**不需要**关联创作者账号；领取后**不会**获得项目、历史或校订的任何访问权。

### 5.3 停用渠道

- 所有者可停用任一渠道；管理人**只能**停用自己直接划出的下级渠道。
- 停用覆盖**整条下游分支**：其中的新划拨、新邀请立即被拒。
- 被停用分支里仍**空闲**的名额退还给该分支的**直接上级**。
- 已发出的有效邀请仍能领取，已领权益仍可读。
- 分支里的**待领邀请**之后到期时，名额退给**原渠道沿原路径最近仍有效的上级**（如果整条链都停用，则回到所有者的根渠道）。
- 停用是幂等的：重复停用返回 `idempotent_replay: true`，不会重复退还名额。

### 5.4 典型调用流程

所有者发布刊期（`idempotency_key` 让重试不重复建刊期）：

```bash
curl -sS -X POST http://127.0.0.1:8000/projects/$PROJECT_ID/issues \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"source_revision":1,"total_quota":100,"idempotency_key":"edition-2026-11"}'
```

所有者把 30 个名额划给渠道管理人：

```bash
curl -sS -X POST http://127.0.0.1:8000/issues/$ISSUE_ID/channels \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"manager_username":"channel_bob","quota":30,"idempotency_key":"alloc-1"}'
```

渠道管理人从自己的渠道邀请读者（`ttl_seconds` 即领取截止）：

```bash
curl -sS -X POST http://127.0.0.1:8000/issues/$ISSUE_ID/channels/$CHANNEL_ID/invitations \
  -H "Authorization: Bearer $MANAGER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"reader_username":"reader_carol","ttl_seconds":86400}'
```

读者查看并领取，然后阅读：

```bash
curl -sS http://127.0.0.1:8000/me/issue-invitations -H "Authorization: Bearer $READER_TOKEN"
curl -sS -X POST http://127.0.0.1:8000/issues/invitations/$INV_ID/accept -H "Authorization: Bearer $READER_TOKEN"
curl -sS http://127.0.0.1:8000/issues/$ISSUE_ID/read -H "Authorization: Bearer $READER_TOKEN"
```

所有者查看各渠道余量：

```bash
curl -sS http://127.0.0.1:8000/issues/$ISSUE_ID/channels -H "Authorization: Bearer $OWNER_TOKEN"
# {"unallocated_owner_quota":70,"quota":{"free":100,"held_by_invitations":0,"claimed":0},
#  "channels":[{"id":2,"parent_id":1,"manager":{...},"available":30,...}]}
```

停用某渠道（整条下游生效，空闲名额回到直接上级）：

```bash
curl -sS -X POST http://127.0.0.1:8000/issues/$ISSUE_ID/channels/$CHANNEL_ID/disable \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

### 5.5 并发、重启与幂等

- 所有名额变动（建刊期、划拨、发邀、停用、接受）都在 `BEGIN IMMEDIATE` 写事务中完成，SQLite 串行化写入，名额计数、held、权益不会出现超额或半成品。
- `total_quota` 与各渠道 `available` 有非负约束；发邀/划拨不足时返回 `409 INSUFFICIENT_QUOTA`，不产生记录。
- 到期名额在**每次读写该刊期时**惰性结算（`_settle_expired`），不依赖人工清理；因此截止一过，该名额立即可用。
- 建刊期、划拨、发邀可带 `idempotency_key`，记录与效果在同一事务写入：重试返回原结果、不重复生效；同一 key 换不同请求体返回 `409 IDEMPOTENCY_CONFLICT`。
- 自然唯一约束兜底：`UNIQUE(issue_id, reader_id) WHERE status='pending'`（每刊期每读者一条待领邀请）、`UNIQUE(issue_id, reader_id)`（权益）、`UNIQUE(invitation_id)`（一张邀请只产生一份权益）。
- 全部状态落盘，服务重启后名额、邀请、权益一致；`scripts/verify_issues_restart_concurrency.py` 用真实 uvicorn 重启与并发请求验证。

### 5.6 错误码补充

| HTTP | `error.code` | 触发场景 |
|---:|---|---|
| 403 | `NOT_PROJECT_OWNER` | 非项目所有者尝试发布/列出该项目的刊期 |
| 403 | `ISSUE_ACCESS_DENIED` | 既非所有者又非该刊期渠道管理人 |
| 403 | `NOT_CHANNEL_MANAGER` | 非渠道管理人尝试从该渠道划拨/发邀 |
| 403 | `CANNOT_DISABLE_CHANNEL` | 管理人尝试停用非自己直接划出的渠道 |
| 403 | `NOT_INVITED_READER` | 非被指定读者尝试领取邀请 |
| 403 | `ISSUE_NOT_CLAIMED` | 未持有权益却尝试读取刊期 |
| 404 | `ISSUE_NOT_FOUND` / `CHANNEL_NOT_FOUND` / `ISSUE_INVITATION_NOT_FOUND` / `REVISION_NOT_FOUND` | 目标不存在 |
| 409 | `INSUFFICIENT_QUOTA` | 渠道可用名额不足 |
| 409 | `CHANNEL_DISABLED` | 从已停用渠道划拨/发邀 |
| 409 | `ROOT_CHANNEL_NOT_DISABLEABLE` | 尝试停用所有者的根渠道 |
| 409 | `ALREADY_ENTITLED` | 该读者已持有本刊期权益 |
| 409 | `IDEMPOTENCY_CONFLICT` | 同一幂等 key 复用于不同请求体 |
| 409 | `MULTIPLE_SOURCE_CHANNELS` / `NO_USABLE_CHANNEL` | 不指定渠道时管理人有多/无可用渠道 |
| 410 | `ISSUE_INVITATION_EXPIRED` | 领取截止已过，名额已释放 |

---

## 6. 错误响应说明

所有业务错误使用统一信封：

```json
{"error": {"code": "MACHINE_CODE", "message": "可读说明", "details": {}}}
```

请求体校验失败时 `code=VALIDATION_ERROR`，`details.issues` 含 Pydantic 错误。

| HTTP | `error.code` | 触发场景 / 客户端处理 |
|---:|---|---|
| 401 | `MISSING_SESSION` | 没带 Bearer token；先登录/注册 |
| 401 | `INVALID_SESSION` | token 不存在或服务端会话已失效 |
| 401 | `SESSION_EXPIRED` | 会话超过本地有效期；重新登录，关联关系不受影响 |
| 401 | `INVALID_CREDENTIALS` | 用户名或密码错误 |
| 403 | `PROJECT_ACCESS_DENIED` | 项目存在，但当前账号不在 owner 的连通分量中；解除后用旧 ID 访问会得到它 |
| 403 | `NOT_EDGE_PARTICIPANT` | 非边端点账号尝试删除该边 |
| 403 | `NOT_INVITED_ACCOUNT` | 指定了被邀请人，但其他账号尝试确认/查看 |
| 404 | `PROJECT_NOT_FOUND` | 项目 ID 根本不存在 |
| 404 | `INVITATION_NOT_FOUND` | 邀请 token 不存在 |
| 404 | `INVITEE_NOT_FOUND` | 创建邀请时指定的用户名不存在 |
| 404 | `EDGE_NOT_FOUND` | 边 ID 不存在 |
| 409 | `USERNAME_TAKEN` | 用户名已注册 |
| 409 | `INVITATION_CONSUMED` | 邀请已由两个账号确认并消费；不能重放成新边，details 含 `consumed_edge_id` |
| 409 | `REVISION_CONFLICT` | 使用 `If-Match: <revision>` 时版本已变化 |
| 410 | `INVITATION_EXPIRED` | 邀请超过 `ttl_seconds`；不能确认，只能新建邀请 |
| 422 | `VALIDATION_ERROR` | 字段长度/格式错误，或 PATCH 没有任何可更新字段 |
| 503 | `DB_BUSY` | SQLite 锁等待超时；响应可安全重试，正常压测下 busy timeout 会消化竞争 |

设计上，**项目存在但无权访问返回 403，项目不存在返回 404**，便于自动脚本明确验证“ID 仍存在但权限已撤销”。如果希望隐藏资源存在性，可把该 403 改成 404；鉴权和一致性逻辑不需要变化。

---

## 7. 并发与重启一致性

### 7.1 为什么不会“先通过权限、后用旧资格提交”

项目修改不是先在事务外鉴权再更新，而是在同一个事务中：

1. `BEGIN IMMEDIATE`，先取得 SQLite 写锁；
2. 在当前已提交图上执行递归可达性检查；
3. 读取当前 `graph_state.version`；
4. 更新项目；
5. 插入只增历史，包含真实 actor 和 `graph_version`；
6. 一起 `COMMIT`。

关联/删边也使用 `BEGIN IMMEDIATE`，并在同一事务内修改边、递增 `graph_state.version`、写 `graph_events`。SQLite 写事务串行化，因此只有两种顺序：

- 修改事务先拿到写锁：它按关联仍存在的图鉴权并提交；之后删边才能提交。该次修改合法。
- 删边事务先提交：修改事务拿到写锁后会看到已删除的边，返回 `403`，不会更新项目或写历史。

不存在“鉴权时有关联、提交前边被删，但修改仍提交”的交错。

历史查询使用 `BEGIN` 快照，鉴权查询和历史读取处于同一一致性快照中，避免读取路径出现类似 TOCTOU。

### 7.2 多条图变化不会留下部分结果

每次建边/删边都在一个事务里同时完成：

- `association_edges` 插入或设置 `deleted_version/deleted_at`；
- `graph_state.version + 1`；
- 插入对应 `graph_events`。

校验脚本会断言：

- `graph_state.version == max(graph_events.version)`；
- `graph_events.version` 从 1 连续，无缺口；
- `linked` 事件对应边的 `created_version`；
- `unlinked` 事件对应边的 `deleted_version`。

因此不会出现图版本已增加但事件/边只写了一半的状态。

### 7.3 用 graph version 做事后独立证明

每条项目历史记录保存 `graph_version`。验证脚本打开 SQLite 文件，按版本重建当时有效边：

```text
created_version <= v
且
(deleted_version IS NULL 或 deleted_version > v)
```

然后从该历史的 `actor_id` 做 BFS，断言 `owner_id` 必须可达。这会检查每一条成功写入，而不只是查看 HTTP 状态码。

一次实际运行的输出示例（数字随调度变化）：

```text
Checking concurrent invitation confirmation retries...
  exactly two distinct confirmations and one edge survived concurrent retries
Checking concurrent DELETE retries for one edge...
  one real deletion; five concurrent retries were idempotent replays
Restart #1: stopping uvicorn and starting it against the same DB
  restart preserved sessions, edge, project, and actor history
Running write/unlink concurrency cycles...
  {'successful_shared_writes': 95, 'denied_racing_writes': 305}
Restart #2 after final split...
  denial persisted across restart; owner access remained
Database graph/history invariant passed:
  {'revisions_checked': 97, 'shared_revisions_checked': 96,
   'final_active_edges': 1, 'graph_events_checked': 23}

PASS: restart, ownership, actor history, concurrency, and revocation checks
```

其中 `final_active_edges: 1` 来自并发邀请测试创建的另一条边；被专门检查的 A-B 主测试边断言为 0。

### 7.4 重启验证内容

`verify_restart_concurrency.py` 会：

1. 找空闲端口；
2. 用临时文件数据库启动真实 uvicorn 子进程；
3. 注册 A/B、建项目、关联、产生共享修改；
4. `SIGTERM` 停止服务；
5. 用同一个 SQLite 文件重启；
6. 用旧 token 调 `/me`，验证会话未撤销；
7. 验证边、项目、历史仍在；
8. 多轮 8 写线程 × 5 次 PATCH 与删边并发；
9. 每轮删边提交后立即断言 Bob 的 GET/PATCH 都是 403；
10. 再次重启，断言拒绝状态和 owner 访问仍一致；
11. 离线检查归属、历史 actor、graph version、邀请唯一确认、边唯一性和图事件连续性。

---

## 8. 目录结构

```text
creator-link-backend/
├── app/
│   ├── main.py          # FastAPI 路由、事务内授权、curation
│   ├── issues.py        # 限量刊期：快照、渠道树、名额、邀请、领取
│   ├── deps.py          # 共享的 Bearer 会话依赖
│   ├── db.py            # SQLite schema、WAL/busy_timeout、事务上下文
│   ├── schemas.py       # 请求模型；不接收 owner_id 更新
│   ├── security.py      # PBKDF2 密码哈希、随机 token、SHA-256 token 存储
│   └── errors.py        # 统一业务错误
├── scripts/
│   ├── wb_common.py
│   ├── demo_two_sessions.py
│   ├── demo_three_chain.py
│   ├── demo_issues.py
│   ├── verify_restart_concurrency.py
│   ├── verify_issues_restart_concurrency.py
│   └── run_all_checks.sh
├── tests/
│   ├── test_workbench.py
│   └── test_issues.py
├── requirements.txt
└── README.md
```

## 9. 生产化备注

这是满足本地可运行要求的简洁实现。若进一步上线，可在不改变核心事务模型的前提下补充：

- HTTPS 与更完整的 session 轮换/撤销；
- refresh token、登录限流和审计日志；
- 对大内容字段做大小限制与对象存储；
- 定期清理过期邀请和已删除边版本；
- 备份 SQLite/WAL，或迁移到 PostgreSQL 并使用行级锁/可序列化事务。
