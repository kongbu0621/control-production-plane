# 运行记录核心公共接口

适用版本：`0.1.0a5`，命令 schema 为 `production-plane-v1`，本地存储格式为 `production-plane-store-v1`。包含本地运行记录、静态 CLI 适配、候选及明确的验收决定；存储格式保持兼容，使用新功能后应继续用本版读取。

## 使用边界

Linux、Python 3.12、单个受信本地用户，在支持 SQLite 和文件锁的本地持久文件系统使用。调用者须保存运行目录并沿原目录恢复。原始输入、调用结果和事件保存在私有目录；公开示例只有合成内容。

核心记录受信调用方提供的身份、准备结果和观察，不认证外部事实。实际调用前，适配器必须核对真实部署、权限、能力与完整请求 schema；`observed_binding` 不能由适配器不经核对地照抄。`evidence_ref` 是调用者维护的证据引用，存入字符串不证明证据真实或已下载。

`LocalHandAdapter` 通过安装的公共 CLI 调用 Unix socket，管理有限的子进程、原始双流、证据下载和完整协议校验；`ArtifactAdapter` 调用公共归档 CLI 并核对实际读回内容。它们面向受信且不派生后台进程的所选 CLI。配置和新增命令见[适配器使用](ADAPTERS.md)；合成任务或本地恢复建议不是现场执行证明。

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
| `store.perform(action_id, observed_binding, invoke, *, preflight=None, knowledge=None)` | 持久记录下发边界后调用受信适配器一次，再记录响应；不自动重发 |

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
- `SELECTED` 的记录格式容纳 1–16 项；本版实际采用和提交只支持其中一个准确版本，并要求原 K 使用回执。旧版仅填 `SELECTED` 字符串的计划不能直接提交。每项恰含 `knowledge_id`、`version_id`、`content_sha256`、`admission_ref`、`scope`。
- `reason` 必填。必需知识不处于 `SELECTED` 时阻止提交；可选知识缺席时保留原因。记录选用信息不等于本核心执行了知识查询或授予准入。

`max_attempts` 为 1–10 次业务提交额度；`max_actions`、`max_queries`、`max_cancellations` 各为 1–1000。准备和提交消耗 actions，提交还预留一个业务额度。查询、取消使用独立额度，业务截止后仍可有界进行。`max_response_bytes` 为 1–262144；核心会检查保存的响应大小，适配器还须在读取响应前限制内存和 I/O。

### 其他操作的 body

| operation | 必填字段与含义 |
| --- | --- |
| `cancel` | `run_id`、`reason`；记录停止继续推进的意图，不宣称远端退出 |
| `revise` | `run_id`、`expected_revision`、`plan`；提交前或原执行已退出且失败后可修正，旧调用需闭合；产生新尝试和原任务 ID，保留旧记录 |
| `queue` | `run_id`、`attempt_id`、`action_id`、`kind`；kind 为 `prepare`、`submit`、`query` 或 `cancel`。仅 prepare 可额外传 `retry_failed=true`，原任务和 inputs 不变 |
| `dispatch` | `action_id`、`observed_binding`；记录可能已下发；准备/提交要求有效执行 lease。仅供库的适配层使用，CLI 拒绝此命令 |
| `respond` | `action_id`、`response`（对象）、`call_closed`（布尔值）；保存原响应，或补记传输失败后核实的响应；准备的 `READY` 需含完整 `request` |
| `close_call` | `action_id`、`evidence_ref`；已记录响应后，凭已核对的进程退出/流完成证据补记调用闭合 |
| `observe` | `run_id`、`attempt_id`、`operation_id`、`request_digest`、`remote_state`、`outcome`、`exited`、`evidence_state`、`evidence_ref` |
| `abandon` | `action_id`、`reason`；只放弃尚未下发的 `QUEUED` 动作，不消耗业务额度 |
| `preflight_failure` | `action_id`、`code`；保存下发前的本地校验失败，保留同一待发动作和额度 |
| `not_started` | `action_id`、`evidence_ref`；仅限已证明调用未启动且无远端影响的失败，退还该次额度；超时/丢响应不得使用 |
| `confirm_not_accepted` | `action_id`、`operation_id`、`request_digest`、`evidence_ref`；提交已返回错误后，凭独立未接纳证明释放业务预留，保留原调用与失败；详见恢复语义 |
| `confirm_stopped` | 为已停止的任务独立副本记录维护退出证明，保留原远端观察与业务未知结果；详见恢复语义 |
| `candidate` | `run_id`、`attempt_id`、`artifact_id`、`version_id`、`source`、`readback_ref`、`receipt_sha256`；记录经实际读回核对的成果 |
| `decide` | `run_id`、`attempt_id`、`candidate_digest`、`actor`、`decision`、`checks`、`reason`；明确接受或拒绝当前候选 |
| `learning_candidate` | `run_id`、`attempt_id`、`candidate_id`、`explanation`、`evidence_refs`、`artifact_versions` |

`revise` 至多保存 100 个计划修订；提交前修正不消耗业务尝试额度。已取消运行不能继续提交或修订；未知提交不能通过修订换 ID 逃避核对。原执行成功但当前候选已有明确 `REJECT` 决定时，也允许修订；已接受或尚未作决定的成功任务不能重做。旧候选、验收决定和使用记录随原修订保留。

`observe` 的 `remote_state` 为 `NOT_FOUND`、`RUNNING`、`TERMINAL`；`outcome` 为 `UNKNOWN`、`SUCCEEDED`、`FAILED`、`CANCELLED`。非终态必须保持 UNKNOWN；`exited=true` 只允许终态。`evidence_state` 为 `UNKNOWN`、`STAGING`、`SEALED`，与业务结果分开。查询未找到不会释放原提交额度；取消请求不能覆盖先完成的业务结果。此接口保存适配器核对后的观察，不代替读取原始执行证据。

学习候选自动绑定目标摘要、计划修订、执行任务和部署绑定，至少提供一项来源引用。`artifact_versions` 每项恰含 `artifact_id`、`version_id`。初次创建返回 `delivery=UNCONFIGURED`、`admission=NOT_REQUESTED`；同候选 ID 改内容拒绝。投递后 `get_candidate` 另含当前状态与 `delivery_record`，历史创建回执不改写。

## 候选、使用记录与决定

`candidate.source` 是公共归档读回的 `lh-artifact-source-v1`，包含 producer、operation_id、request_digest、status_sha256、seal_id、seal_sha256 和 workspace。核心核对它与当前尝试及计划的对应关系；`ArtifactAdapter.read` 还读取真实文件，核对归档回执的准确版本和文件摘要、原任务及认证 seal、前后内容、补丁与固定验证结果。status_sha256 指归档保留的状态快照；后续认证查询可能产生不同的观察摘要，两者不互相覆盖。旧记录的摘要核对及回执接续见[适配器说明](ADAPTERS.md)。核心接受受信操作者的明确记录，不把该接口当作防止同一用户伪造证据的鉴权系统。

候选要求观察为已退出、成功且已封存。当前适配器只接纳可无歧义核对 Git porcelain 的 ASCII 路径（字母、数字、`_./-`）；额外源码变更、补丁变化或内容不等于计划均拒绝。`receipt_sha256` 是实际核对过的 source、文件清单、消费后内容摘要和补丁摘要组成对象的规范化 SHA-256。

候选自动绑定 goal_digest 与 revision，`candidate_digest` 为候选整个对象的规范化摘要。相同候选再次读回会追加新的 readback_ref 使用事件，保持原候选身份和决定；不同候选不得覆盖。这里的 candidate 是**成果候选**，与 `learning_candidate` 知识候选不同。

`decision` 为 `ACCEPT` 或 `REJECT`。`checks` 按原 goal.acceptance 顺序逐项包含 `{condition, passed, evidence_ref}`；不能遗漏或换掉条件，ACCEPT 要求全部通过。actor 和 reason 必填，决定绑定准确候选摘要与当前尝试。成功执行、归档或读回不会自动写 ACCEPT；同内容决定可幂等重放，旧决定不能套到新修订。

## 调用与恢复语义

`confirm_not_accepted` 的 body 恰含 `action_id`、`operation_id`、`request_digest`、`evidence_ref`。它用于真实提交调用已退出、双流完整且返回错误之后，由受信操作者另行核实原执行端确实未接纳任务的接续。证明必须绑定原部署、主体、任务及请求；错误码、超时或一次 NOT_FOUND 都不能单独成为证明，核心不会自动生成这种结论。当前仅接受已闭合提交的 `{remote_error}` 错误响应，或适配器附有 `call_id` 与 `response_sha256` 的同类响应。

该操作保留原 RESPONDED 动作、错误响应和已消耗调用额度，另附 `non_acceptance` 证明；只释放未被实际接纳的业务尝试预留。原尝试和 operation ID 不变，后续显式排队一个新提交动作，重新核对准备、授权、预算与取消状态。其他调用未闭合、请求错位、曾观察到运行/终态或证据封存时拒绝；已保存响应中的观察同样参与检查，即使进程在 respond 后、observe 落账前退出，或后来又查询到 NOT_FOUND，也不能忽略已返回的执行事实；证明与额度释放在同一事务中保存。同一证明重放不重复释放，也不清除后来提交；不同证明不能覆盖原证明。这不同于证明进程/传输从未启动的 `not_started`。

`confirm_stopped` 用于原执行窗口已结束、公共状态仍有缺口的 `workspace.edit`。body 恰含 `run_id`、`attempt_id`、`operation_id`、`request_digest`、`binding_digest`、`evidence_ref`、`future_start_blocked`、`tree_exited`、`writers_stopped`、`effect_scope`。binding_digest 是本运行 binding 的规范化摘要；三个退出字段必须为 true，effect_scope 必须为 TASK_OWNED_COPY。受信维护者须核实原部署、请求、下发单位和进程树身份，原启动能力已被封锁，全部写入者已停止，副作用仅在原任务独立副本；单个 PID 不存在、控制器退出或错误码本身不足以证明。所有原调用必须已闭合。

证明独立存入 maintenance_stop，不改写远端 observation、UNKNOWN、原任务、调用记录或预算，不生成候选或 ACCEPT。它只允许其他明确创建的运行通过此旧运行的执行占用检查；不会重新开放旧任务的准备/提交或退还业务额度。新运行仍需独立的目标、计划、授权、预算和执行检查。重复相同证明幂等，不同证明拒绝覆盖；recover 返回 REVIEW_STOPPED_RESULT_GAPS，继续提示原结果缺口。本版不据此放行任意外部副作用的未知任务。 停止证明与释放原业务预留互斥：confirm_not_accepted 和提交的 not_started 不能撤销它。准备、提交的排队/下发及未知结果修订也直接检查停止证明，兼容旧版本已错误释放 submission_recorded 的记录；不会重放旧任务。后来取得明确退出失败或成功但拒绝候选的观察时，仍可按原修订条件保存旧尝试并创建新尝试。

动作从 `QUEUED` 经事务变为 `MAY_HAVE_RUN`，才允许适配器调用。响应保存后为 `RESPONDED`；`call_closed=false` 仍表示调用或采集未闭合。未下发或有明确未启动证明的动作为 `NOT_SENT`。进程在下发边界两侧被杀时，本地记录只证明已知事实，不能从缺响应推断无远端影响。

`perform` 的 `preflight(payload)` 只做下发前检查；抛出 `ControlError` 会留档且不计调用额度。`invoke(payload)` 返回恰含 `response` 与 `call_closed` 的对象。它必须自行执行有限 I/O、完整协议验证和进程/双流跟踪；本版核心不监视适配器派生的子进程，也不保证回调超时后已经停止。

适配器若证明连进程/传输都没有启动，可抛 `CallNotStarted(evidence_ref)`；核心保存失败并退还额度，同一任务可重新排队。普通异常按结果未知处理，保留调用缺口；不会因为异常类型是 `TimeoutError` 或 `OSError` 就推断无执行。

普通异常保留为 `RESPONDED` 动作中的 `{"transport_error":"异常类型"}`；这个只有一个字符串字段的对象是核心保留的传输失败标记，不是已确认的远端响应，适配器不得用它包装已收到的业务结果。后续取得真实响应，可用新的本地 `request_id` 向原 `action_id` 提交 `respond`；取得明确未启动证明，则可提交 `not_started`。这两种接续只补记事实，不重新调用原动作。原失败事件和成功回执保留；已确认的普通响应不能替换。原回执重放不回写当前响应，也不再次退还额度。

响应补记与额度退还仍是本地事务，提交返回 unknown 时沿原请求核对。补记响应仍受大小限制；准备 READY 继续核对当前尝试、原计划与请求摘要，且任务尚未提交。完整协议与外部身份仍由适配器核对。迟到的旧 READY 保存在原动作上，当前可提交请求按准备下发顺序重新判定，不能越过后来 FAILED、PREPARING 或新 READY。已记录的调用闭合不能改回未闭合；准备调用没有退出和双流证明时仍保留未闭合状态，阻止下一次准备或提交。

计划修订后才取得旧准备或查询调用的未启动证明，仍可向原动作提交 `not_started`。它只退还原调用占用的 actions 或 queries 额度、记录证明；不改变当前尝试、任务 ID、READY、提交/取消状态，也不回写修订时保存的历史快照。旧失败事件继续保留。同一证明重放不再次退款，已确认的普通响应仍不能改报未启动。提交和取消的未启动证明仍要求属于当前尝试，不能借旧提交证明清除新任务的提交事实。

提交意图落账后，并发查询可能先返回 `NOT_FOUND`。这项观察本身不退款、不允许重提；若随后取得独立的未启动证明，且同一尝试的当前观察和历史均没有运行、终态或证据已封存等矛盾事实，仍可记录证明并退还该次额度。原查询、请求和任务 ID 保留。曾观察到运行或终态后再查询不到，也不能据此退还额度；重复原证明只返回旧回执，不撤销后来已记录的提交。

同一动作 ID 不会被 `perform` 再次调用。核心已记录可能提交后，本版不自动重发同一 submit；先查询原任务。准备响应未知时，真实适配器需核对原准备及旧调用状态，再记录响应/闭合，不能改用新 UUID 猜测结果。准备返回 `PREPARING` 后，新轮询动作仍保留同一任务及原 inputs。

接收 `READY` 时校验操作 ID、任务类型、profile、inputs、有效期与请求摘要；保存完整请求，提交前原样取出。完整的 Local Hand schema 由安装依赖验证，CLI 适配器还核对来源、能力、socket 服务身份及绑定配置；样例里的合成 READY 不是可下发的真实请求。

新的准备动作一旦落下发意图，就清除当前可提交请求，等待该轮重新得到有效 `READY`；旧请求仍完整保存在原动作响应中。该轮返回 `PREPARING`、`FAILED`、调用中断或结果未知时，不能继续提交旧 READY。本地预检失败或下发事务回滚时，原请求保持不变。

若下发意图已经落账，但随后取得明确的调用未启动证明，`not_started` 在退还额度的同一事务中，从同一尝试的准备历史恢复仍适用的 READY。核对按下发意图的落账顺序进行，只跳过已证明未启动的准备，不越过失败、未就绪或未知调用恢复更早请求，也不跨尝试取旧请求。恢复后仍检查有效期、取消、知识、未决调用及预算；不因一次本地启动失败额外消耗准备额度。

新的提交排队和真正下发均核对上述准备历史，不能只相信 `prepared_request` 缓存。旧版本可能留下“后续准备未就绪、缓存仍为 READY”的记录；打开和拒绝提交不修改这些原件。沿原任务重新取得有效 READY 后可继续；同 ID 同内容仍可返回原排队回执，但旧成功回执不能跳过下发时的当前核对。

遍历准备历史时，先核对下发事件、动作和原排队事件的关联，再判断尝试和动作类型。遇到缺失关联、身份不一致或不可解释的准备记录，返回 `STORE_INVALID`，不能静默跳过后恢复更早 READY。依赖这次核对的本地事务整体不提交；保留原记录和调用证据，核对损坏原因。本检查覆盖实际读取到的关联，不是完整数据库审计或历史重建。

取消 payload 使用 `operation_id`、`expected_request_digest` 和 `target={"kind":"job"}`，与 Local Hand 公共接口一致。下发时再次核对原提交及摘要。首批实现中以 `request_digest` 排队的旧取消动作保留，但拒绝下发；确认仍为 `QUEUED` 后可用 `abandon` 保留为未发送，再为同一原任务排队新的取消动作。已经可能发送的动作继续核对原结果，不能按此方式替换或重做业务。

一次 `perform` 只取得一个待发动作的调用权，不自动循环推进任务。恢复查询不隐式执行。另一个运行有未闭合调用或未退出业务时，不允许新的准备/提交；原任务查询和取消仍保留独立入口。

## 持久化、限额与错误

`identity.json` 与 SQLite 内身份必须匹配，文件只允许当前用户访问；拒绝文件替换、非普通文件、多硬链接、缺失和不支持格式。调用前后由独立 SQLite 短事务保存；本地成功回执丢失后，使用相同 request ID 和内容核对重放。SQLite 提交返回错误可能为 `commit_state=unknown`，不能换 key 重做。

请求 UTF-8 JSON 及规范化 JSON 各最多 256 KiB、20 层、20000 个节点；拒绝重复键、非整数数字、非有限数、非法 Unicode 和超出安全范围的整数。每个运行最多 3000 条动作；数据库按页数预留事务空间，限制在 256 MiB 范围，临时 journal 另需磁盘空间。不会自动裁剪历史或清理未知动作来腾出额度。

目录只能位于受信本地文件系统；不支持 NAS 上活动数据库、多机写入、多租户鉴权、任意数据库回滚后的自动恢复、任意掉电保证或数据库格式迁移。完整控制目录须一起保留；本版没有备份/恢复命令。已识别的缺失、冲突和损坏应先核对原记录，不重新建立空库替代。

库抛出 `ControlError(code, message, commit_state=...)`；主要代码为 `INVALID_INPUT`、`LIMIT`、`CONFLICT`、`NOT_FOUND`、`NOT_READY`、`PENDING`、`BUSY`、`LEASE_REQUIRED`、`IDENTITY_CONFLICT`、`STORE_INVALID`、`STORAGE_ERROR`、`CALL_UNKNOWN`、`CALL_NOT_STARTED`。CLI 成功输出单行 JSON 到 stdout、退出 0；失败输出结构化错误到 stderr、退出 2。输出失败不会撤销已提交记录，仍须按原请求核对。

`perform` 在确认下发意图落账后，若适配器、响应校验或响应持久化抛出 `ControlError`，其属性和 `as_dict()` 额外包含原 `action_id` 与 `dispatch_recorded=true`。`commit_state` 保留报错步骤自身的状态：例如响应未提交或提交结果未知，都不表示此前的下发没有发生。使用原动作读取当前事实，不凭错误重放；`dispatch_recorded` 也不证明业务实际执行成功，未启动证明仍按 `CALL_NOT_STARTED` 和原记录核对。该字段缺失不构成未下发证明，特别是下发事务本身返回 unknown 或进程没有返回时。

## 可选 K 接入

`control_production_plane.knowledge.KnowledgeAdapter` 的 `query`、`selection`、`adopt`、`feedback`、`resume` 通过 K 公共库工作，完整合同见[运行知识适配器](KNOWLEDGE.md)。

新增本地操作 `knowledge_intent`（需执行租约）和 `knowledge_ack`。前者的 body 恰含 `run_id revision knowledge_store_id request notes`，后者恰含 `run_id call_id receipt`；应由适配器构造。它们将原请求、准确尝试和回执存入运行的 `knowledge_calls`，仍与 P 事件和请求回执同事务提交。`recover.pending_knowledge_calls` 列出未确认的 K 调用，不自动发送。调用者仍属于同一受信域，直接写入的回执不是密码学认证。

新 pending 调用包含 `receipt_capacity_version=1`，表示已在运行记录中预留成功回执和取消记录容量；确认后移除此字段。旧记录可以没有该字段，恢复按[知识接入边界](KNOWLEDGE.md#中断与边界)处理。`knowledge_ack` 校验 K 公共回执的精确结果字段、布尔 `reused` 和非负安全整数时间戳；额外结果字段不被默认为受支持。容量检查中的占位值仅在内存计算，不是持久证据或实际发生的取消。

无需更改原数据库表；旧运行在使用新功能前保持原状。使用新字段后的存储必须继续用 `0.1.0a4` 或兼容后续版本，不能降级到没有采用检查的旧执行器。

### 新提交的知识检查

`0.1.0a4` 的 `store.perform` 与 `LocalHandAdapter.perform` 接受可选 `knowledge=KnowledgeAdapter(store, config)`。SELECTED 任务的新 submit 必须提供绑定当前 P 句柄的适配器，并取得新的当前状态检查；直接 JSON dispatch 无法代替该调用。普通未选知识任务、query 和 cancel 的原调用方式兼容。

`execute-action` 增加 `--knowledge-adapter FILE`，文件格式与已有知识命令的 `--adapter` 相同。`action.knowledge_check` 是随当次 dispatch 保存的历史观察，不能重放为许可。检查失败保留 QUEUED 和 `preflight_failure`，不花业务额度；已经 MAY_HAVE_RUN 的动作不重新提交。详细窗口和错误语义见[知识接入](KNOWLEDGE.md)。旧安装没有该检查，执行这类任务的进程应统一升级；本版没有改写旧账本或强制迁移数据库。

SELECTED 提交还会在 dispatch 事务内检查原请求、知识观察及完整 `max_response_bytes` 是否共同满足动作记录的 256 KiB 上限。容量不足返回 `LIMIT`，不登记提交、不消耗执行额度、不调用业务；既有动作与原采用保留。该字节容量检查不改变响应 schema、结构限额或实际 I/O 失败的处理。

## 学习候选投递（0.1.0a5）

`control_production_plane.learning.LearningAdapter(store, config)` 使用与 KnowledgeAdapter 相同的明确 K 配置。其中 `scope` 沿用 P 配置的 1–128 个 ASCII 字符的标识约束，`p`、`pk` 等短作用域有效；不要套用 K 材料 ID 至少三个字符的约束。`deliver(candidate_id, proposal)` 保存原意图并推进两步；`resume_delivery(candidate_id)` 只用保留请求接续。CLI 对应 `deliver-learning --candidate-id ID --proposal FILE --adapter FILE`、`resume-learning --candidate-id ID --adapter FILE`；`show-learning --candidate-id ID` 读取原候选及当前投递记录。

proposal 恰含 `kind content conditions context gaps material_ids`。kind 为 1–64 UTF-8 bytes，content 为 1–65536 bytes，均拒绝 NUL；context/conditions 沿用 K 的最多 16 项字符串映射。gaps 最多 15 条非空、无 NUL、至多 512 bytes 的说明；material_ids 为 0–7 个不同的已有 K 材料 ID，额外加上本次发布的运行材料。完整请求和投递记录仍受 P 的 256 KiB ASCII JSON/结构限额约束，扩展转义后的容量不足在首次 K 写入前拒绝。

`delivery_record` 包含 `schema=production-learning-delivery-v1`、knowledge_store_id、scope、proposal、source_sha256、requests 和 receipts。requests/receipts 分别以 publish_material、propose 为键；每个成功回执保存 K 完整身份、请求指纹及结果。RECEIVED 需要两个回执，部分完成保留 PENDING。返回值包含 candidate_id、delivery、admission 和 receipts；K 的 candidate_id/candidate_sha256 在 propose 回执中。

适配器使用两个本地命令（均需原执行租约）：`learning_intent` 的 body 为 candidate_id、knowledge_store_id、scope、proposal；`learning_ack` 为 candidate_id、operation、receipt。它们与 P 事件和本地回执同事务提交，应由适配器构造。直接写入的回执仍处于受信本地调用边界内，不是外部身份认证。`recover` 继续报告生产与知识采用调用；学习投递用原 candidate_id 经 show-learning/resume-learning 接续。

材料保留首次投递时的原尝试观察，晚到的取消、修订或结果不会重写它。候选说明和 evidence_refs 是调用方声明；artifact_versions 需与原尝试已记录成果匹配，但本适配器不重新下载成果原件。额外 K 材料由 propose 核对，缺失时保留已完成发布及待决提案。投递不调用 review/start_use，不修改生产状态或业务预算。使用投递字段后继续用 `0.1.0a5` 或兼容后续版本，不回退旧写入者。
