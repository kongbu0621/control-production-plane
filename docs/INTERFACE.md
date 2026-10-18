# 运行记录核心公共接口

适用版本：`0.1.0a1`，命令 schema 为 `production-plane-v1`，本地存储格式为 `production-plane-store-v1`。这是首批本地核心接口；Local Hand、Ledger 的真实适配和成果验收尚未交付。

## 使用边界

Linux、Python 3.12、单个受信本地用户，在支持 SQLite 和文件锁的本地持久文件系统使用。调用者须保存运行目录并沿原目录恢复。原始输入、调用结果和事件保存在私有目录；公开示例只有合成内容。

核心记录受信调用方提供的身份、准备结果和观察，不认证外部事实。实际调用前，适配器必须核对真实部署、权限、能力与完整请求 schema；`observed_binding` 不能由适配器不经核对地照抄。`evidence_ref` 是调用者维护的证据引用，存入字符串不证明证据真实或已下载。

本版没有内置网络调用器、Local Hand CLI 子进程管理、证据下载、Ledger 归档和验收决定入口。库的 `perform` 是适配器调用边界；本地 CLI 只维护记录。不得把合成回执或本地恢复建议报告成现场执行成功。

## Python 入口

```python
from control_production_plane import initialize, open_store, ControlError, CallNotStarted

store = initialize("/absolute/private/new-control-store")
# 后续使用 open_store 打开原目录，不再次 initialize。
```

| 入口 | 行为 |
| --- | --- |
| `initialize(directory)` | 只创建不存在的目录，返回 `ControlStore`；已有或初始化不完整目录保留 |
| `open_store(directory)` | 检查身份、格式和 SQLite；缺失或损坏不自动初始化 |
| `store.execute(request, *, lease=None)` | 提交本地 JSON 命令；状态、事件和成功回执在同一事务中保存 |
| `store.get_run(run_id)` | 返回当前目标、绑定、计划、尝试、取消意图和累计预算 |
| `store.get_revision(run_id, revision)` | 返回当前修订，或改为下一修订时保留的原计划、尝试身份及结果 |
| `store.get_action(action_id)` | 返回动作的原参数、下发边界、响应和调用闭合状态 |
| `store.get_candidate(candidate_id)` | 读取本地学习候选及其来源、发送与准入状态 |
| `store.events(run_id, *, after=0, limit=10)` | 按全库事件序号分页；每页最多 10 条，下一页用上一页最后一个序号 |
| `store.recover(run_id)` | 一致读取运行、未闭合调用、待发动作和接续建议；不发出外部调用 |
| `store.executor()` | 上下文管理器，取得本控制存储的执行互斥；不持有长期 SQLite 写事务 |
| `store.perform(action_id, observed_binding, invoke, *, preflight=None)` | 持久记录下发边界后调用受信适配器一次，再记录响应；不自动重发 |

返回的数据是独立值；修改返回对象不改变存储。一个实例可重新打开为多个 handle；多个进程的本地变更由短事务串行化。准备和提交需执行互斥；查询、取消意图与原任务取消调用不等待执行互斥。

## JSON 命令

所有写命令的 envelope 恰含以下字段；未知或缺少字段拒绝：

```json
{
  "schema": "production-plane-v1",
  "request_id": "caller:unique-request",
  "operation": "cancel",
  "body": {"run_id": "example", "reason": "Stop further submission"}
}
```

`request_id` 在单个存储中唯一，绑定完整规范化命令。同 ID 同内容返回 `replayed=true` 和原回执；同 ID 不同内容为 `CONFLICT`。回执是历史提交事实，当前状态应重新读取。回执中的 `result` 含本次对象标识；不把旧回执解释为重新调用执行器的许可。

身份字符串长度 1–128，允许 ASCII 字母、数字及 `_.:/-`，首字符须为字母或数字。动作 ID、尝试 ID、执行任务 ID 为小写 UUID v4。时间使用整数 Unix 秒；摘要使用小写 SHA-256。

### create

`body` 恰含 `run_id`、`goal`、`binding`、`plan`、`budget`。可运行完整样例见 [local_records.py](../examples/local_records.py)。

| 对象 | 必填字段 |
| --- | --- |
| `goal` | `text`：目标；`acceptance`：1–32 条非重复的验收条件文本 |
| `binding` | `executor_id`、`authority_id`、`principal_id`、`profile_ref`、`tool_schema_digest`、`archive_id`、`producer` |
| `plan` | `source_ref`、`source_commit`、`relative_path`、`expected_sha256`、`content`、`validation_ref`、`knowledge` |
| `budget` | `max_attempts`、`max_actions`、`max_queries`、`max_cancellations`、`deadline_utc`、`max_response_bytes` |

目标和执行/归档绑定在运行内固定；修正计划使用新修订。`source_commit` 为完整 40 或 64 位小写十六进制提交 ID。`relative_path` 是规范化 POSIX 相对文件路径，不允许绝对路径、回退或反斜杠。`content` 是替换后的完整 UTF-8 文本，允许空文件，最多 16 KiB；来源与文件内容仍需真实适配器重新核对。

`knowledge` 恰含 `required`（布尔值）、`status`、`reason`、`items`：

- `UNCONFIGURED`、`NO_MATCH`、`QUERY_FAILED` 分别表示未配置、没有匹配及查询失败；`items` 必须为空。
- `SELECTED` 绑定 1–16 项选用知识；每项恰含 `knowledge_id`、`version_id`、`content_sha256`、`admission_ref`、`scope`。
- `reason` 必填。必需知识不处于 `SELECTED` 时阻止提交；可选知识缺席时保留原因。记录选用信息不等于本核心执行了知识查询或授予准入。

`max_attempts` 为 1–10 次业务提交额度；`max_actions`、`max_queries`、`max_cancellations` 各为 1–1000。准备和提交消耗 actions，提交还预留一个业务额度。查询、取消使用独立额度，业务截止后仍可有界进行。`max_response_bytes` 为 1–262144；核心会检查保存的响应大小，适配器还须在读取响应前限制内存和 I/O。

### 其他操作的 body

| operation | 必填字段与含义 |
| --- | --- |
| `cancel` | `run_id`、`reason`；记录停止继续推进的意图，不宣称远端退出 |
| `revise` | `run_id`、`expected_revision`、`plan`；提交前或原执行已退出且失败后可修正，旧调用需闭合；产生新尝试和原任务 ID，保留旧记录 |
| `queue` | `run_id`、`attempt_id`、`action_id`、`kind`；kind 为 `prepare`、`submit`、`query` 或 `cancel`，参数由保存的计划/请求生成 |
| `dispatch` | `action_id`、`observed_binding`；记录可能已下发；准备/提交要求有效执行 lease。仅供库的适配层使用，CLI 拒绝此命令 |
| `respond` | `action_id`、`response`（对象）、`call_closed`（布尔值）；保存原响应；准备的 `READY` 需含完整 `request` |
| `close_call` | `action_id`、`evidence_ref`；已记录响应后，凭已核对的进程退出/流完成证据补记调用闭合 |
| `observe` | `run_id`、`attempt_id`、`operation_id`、`request_digest`、`remote_state`、`outcome`、`exited`、`evidence_state`、`evidence_ref` |
| `abandon` | `action_id`、`reason`；只放弃尚未下发的 `QUEUED` 动作，不消耗业务额度 |
| `preflight_failure` | `action_id`、`code`；保存下发前的本地校验失败，保留同一待发动作和额度 |
| `not_started` | `action_id`、`evidence_ref`；仅限已证明调用未启动且无远端影响的失败，退还该次额度；超时/丢响应不得使用 |
| `learning_candidate` | `run_id`、`attempt_id`、`candidate_id`、`explanation`、`evidence_refs`、`artifact_versions` |

`revise` 至多保存 100 个计划修订；提交前修正不消耗业务尝试额度。已取消运行不能继续提交或修订；未知提交不能通过修订换 ID 逃避核对。原任务成功但验收不通过后的修复，需后续验收入口实现；本版只允许确认执行失败后的业务修订。

`observe` 的 `remote_state` 为 `NOT_FOUND`、`RUNNING`、`TERMINAL`；`outcome` 为 `UNKNOWN`、`SUCCEEDED`、`FAILED`、`CANCELLED`。非终态必须保持 UNKNOWN；`exited=true` 只允许终态。`evidence_state` 为 `UNKNOWN`、`STAGING`、`SEALED`，与业务结果分开。查询未找到不会释放原提交额度；取消请求不能覆盖先完成的业务结果。此接口保存适配器核对后的观察，不代替读取原始执行证据。

学习候选自动绑定目标摘要、计划修订、执行任务和部署绑定，至少提供一项来源引用。`artifact_versions` 每项恰含 `artifact_id`、`version_id`。本版只保存本地候选，固定返回 `delivery=UNCONFIGURED`、`admission=NOT_REQUESTED`；同候选 ID 改内容拒绝，不假报已回流或已准入。

## 调用与恢复语义

动作从 `QUEUED` 经事务变为 `MAY_HAVE_RUN`，才允许适配器调用。响应保存后为 `RESPONDED`；`call_closed=false` 仍表示调用或采集未闭合。未下发或有明确未启动证明的动作为 `NOT_SENT`。进程在下发边界两侧被杀时，本地记录只证明已知事实，不能从缺响应推断无远端影响。

`perform` 的 `preflight(payload)` 只做下发前检查；抛出 `ControlError` 会留档且不计调用额度。`invoke(payload)` 返回恰含 `response` 与 `call_closed` 的对象。它必须自行执行有限 I/O、完整协议验证和进程/双流跟踪；本版核心不监视适配器派生的子进程，也不保证回调超时后已经停止。

适配器若证明连进程/传输都没有启动，可抛 `CallNotStarted(evidence_ref)`；核心保存失败并退还额度，同一任务可重新排队。普通异常按结果未知处理，保留调用缺口；不会因为异常类型是 `TimeoutError` 或 `OSError` 就推断无执行。

同一动作 ID 不会被 `perform` 再次调用。核心已记录可能提交后，本版不自动重发同一 submit；先查询原任务。准备响应未知时，真实适配器需核对原准备及旧调用状态，再记录响应/闭合，不能改用新 UUID 猜测结果。准备返回 `PREPARING` 后，新轮询动作仍保留同一任务及原 inputs。

接收 `READY` 时校验操作 ID、任务类型、profile、inputs、有效期与请求摘要；保存完整请求，提交前原样取出。完整的 Local Hand schema、启动绑定和能力验证由后续真实适配器补齐；样例里的合成 READY 不是可下发的真实请求。

一次 `perform` 只取得一个待发动作的调用权，不自动循环推进任务。恢复查询不隐式执行。另一个运行有未闭合调用或未退出业务时，不允许新的准备/提交；原任务查询和取消仍保留独立入口。

## 持久化、限额与错误

`identity.json` 与 SQLite 内身份必须匹配，文件只允许当前用户访问；拒绝文件替换、非普通文件、多硬链接、缺失和不支持格式。调用前后由独立 SQLite 短事务保存；本地成功回执丢失后，使用相同 request ID 和内容核对重放。SQLite 提交返回错误可能为 `commit_state=unknown`，不能换 key 重做。

请求 UTF-8 JSON 及规范化 JSON 各最多 256 KiB、20 层、20000 个节点；拒绝重复键、非整数数字、非有限数、非法 Unicode 和超出安全范围的整数。每个运行最多 3000 条动作；数据库按页数预留事务空间，限制在 256 MiB 范围，临时 journal 另需磁盘空间。不会自动裁剪历史或清理未知动作来腾出额度。

目录只能位于受信本地文件系统；不支持 NAS 上活动数据库、多机写入、多租户鉴权、任意数据库回滚后的自动恢复、任意掉电保证或数据库格式迁移。完整控制目录须一起保留；本版没有备份/恢复命令。已识别的缺失、冲突和损坏应先核对原记录，不重新建立空库替代。

库抛出 `ControlError(code, message, commit_state=...)`；主要代码为 `INVALID_INPUT`、`LIMIT`、`CONFLICT`、`NOT_FOUND`、`NOT_READY`、`PENDING`、`BUSY`、`LEASE_REQUIRED`、`IDENTITY_CONFLICT`、`STORE_INVALID`、`STORAGE_ERROR`、`CALL_UNKNOWN`、`CALL_NOT_STARTED`。CLI 成功输出单行 JSON 到 stdout、退出 0；失败输出结构化错误到 stderr、退出 2。输出失败不会撤销已提交记录，仍须按原请求核对。
