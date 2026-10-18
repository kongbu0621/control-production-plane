# 执行、归档与验收

适用版本 `0.1.0a2`。这些操作使用已安装的公共 CLI；运行记录、配置、调用原件和证据放在私有本地目录。不要用本页的示例身份替代真实部署信息。

## 安装与绑定

先按[使用说明](USAGE.md)安装 P。需要执行适配时，在同一个独立环境中安装核对过的 `infra-local-hand` wheel；需要归档时再安装 `infra-artifact-ledger` wheel。当前兼容所验证的 `0.2.0a1` 包，版本名相同也须核对实际源码或 wheel 摘要；不自动从包索引下载同名包。

核心使用标准库，执行适配依赖 Local Hand 的公共 `contract`、`evidence_client` 和 CLI，归档通过 `local-hand-archive` 使用 Ledger。没有配置这些适配器时，本地记录功能仍可独立使用。

执行配置 JSON 恰含：

| 字段 | 内容 |
| --- | --- |
| `schema` | `production-plane-local-hand-v1` |
| `binding` | 与运行 create.binding 完全相同的 executor_id、authority_id、principal_id、profile_ref、tool_schema_digest、archive_id、producer |
| `command` | 安装的 CLI argv，例如 `["/absolute/runtime/bin/python", "-I", "-B", "-m", "local_hand_jobs.cli"]` |
| `socket` | 既有 Local Hand Unix socket 的绝对路径 |
| `server_uid` | 实际服务进程和 socket 所有者 UID（整数） |
| `policy_digest`、`catalog_version` | 当前授权主体调用 `lh_capabilities` 返回并核对过的准确值 |

配置文件权限为 `0600`，适配器数据目录为 `0700`。P 必须以服务已映射的普通用户身份运行；principal_id 来自该映射的核验，不能随意改名字获得另一主体的权限。命令是显式 argv，不经过 shell，也不继承凭据环境；首个程序须是绝对路径。适配器连接前后核对 socket 身份，并以 SO_PEERCRED 核对服务 UID；配置与首次控制存储绑定后不能原地替换。

先用 `inspect-executor --adapter FILE --data DIRECTORY` 核对能力。真实来源可以通过 Python `LocalHandAdapter.workspace_read(plan)` 核对；准备和提交前也会自动读取源文件，验证来源提交、路径、大小与 SHA-256。源码中有功能不等于服务已开放；目标没有登记时，应先明确必要配置，不能随意选旧来源完成无关任务。

归档配置恰含：

```json
{
  "command": ["/absolute/runtime/bin/python", "-I", "-B", "-m", "local_hand_jobs.artifact_archive"],
  "archive_store": "/absolute/private/artifact-store"
}
```

归档数据目录独立于执行数据目录，首次绑定到同一个 P 控制存储。archive_id 和 producer 来自创建运行时确定的归档身份；所选实际 archive_store 此后固定，不能把另一目录冒充原归档实例。当前不支持跨主机或多用户共享适配器数据。

## 从原计划推进

所有命令先指定 `production-plane --store CONTROL_DIRECTORY`，下面列出子命令。create、queue、cancel、decide 等请求格式见[公共接口](INTERFACE.md)。每一步返回后检查真实状态，不把命令列表当作无条件批处理。

1. `write --request CREATE_JSON`：保存实际目标、验收条件、输入、绑定及预算。随后用 queue 排队 `prepare`，保存返回动作 ID。
2. `execute-action --adapter EXECUTOR_JSON --data EXECUTOR_DATA --action-id ID`：执行该准备动作。PREPARING 沿同一任务及原参数排队新的 prepare 轮询；修正可重试 FAILED 的原因后，显式传 `retry_failed=true`。READY 的完整请求保存在原动作中。
3. READY 有效且仍允许执行时，排队 `submit` 并 execute-action。提交意图落账后只查询原任务，不再次调用同一个动作或新建任务来绕过未知结果。
4. 排队 `query` 并 execute-action，保存新的状态。需要取消时先写 cancel 意图，再排队 cancel 动作；取消回执不能代替退出证明。
5. 已确认退出、成功且封存时，`collect-evidence --adapter EXECUTOR_JSON --data EXECUTOR_DATA --action-id STATUS_ACTION_ID --output NEW_EVIDENCE_DIRECTORY`。STATUS_ACTION_ID 必须是该尝试成功状态对应的原动作。返回 `data` 含 directory、operation_id、status_sha256 和可信 seal。
6. 将上一步 JSON 信封的 **data 对象**保存为私有 EVIDENCE_JSON，调用 `archive-result --adapter ARCHIVE_JSON --data ARCHIVE_DATA --run-id RUN_ID --evidence EVIDENCE_JSON`。保存返回的 artifact_id / version_id。
7. `read-artifact --adapter ARCHIVE_JSON --data ARCHIVE_DATA --run-id RUN_ID --artifact-id ARTIFACT_ID --version-id VERSION_ID --output NEW_READ_DIRECTORY`。适配器核对真实文件、原状态与 seal、任务和输入、前后内容、补丁与验证结果，随后记录候选及本次使用。输出目录必须不存在。
8. 审查读回内容，按原验收条件写 `decide` 请求。候选摘要可用 `control_production_plane.model.digest(store.get_run(run_id)["attempt"]["candidate"])` 计算。只有操作者明确接受才记录 ACCEPT；REJECT 保留拒绝依据，允许在预算内提供修订方案。

各 CLI 成功响应都是 `{status: "OK", data: ...}`；传入下一步骤时使用所需的 data 字段，不能把整个信封当成业务对象。比如把 collect-evidence 的 stdout 保存为私有响应文件后，可在受信本地脚本中 `json.load(...)["data"]` 取得证据描述。

归档证据描述恰含 `directory`、`operation_id`、`status_sha256` 和 `seal`；directory 是绝对路径，seal 恰含 `seal_id` 与 `seal_sha256`。首次保存归档输入前，适配器检查这些字段，并读取原 status.json 核对摘要、任务、请求、成功封存状态、seal 与退出证明。缺项、错路径、摘要或状态不符时，不保存不可变归档输入，也不启动归档 CLI；修正描述或本地可读性后可以继续。该核对不能代替来源认证，摘要和 seal 仍须来自前述 collect-evidence 的可信结果。

已有归档输入继续绑定原描述。成功归档回执可直接复用，resume 从公共归档存储核对恢复；这两条路径不要求原下载文件仍然存在，不能因源文件后来不可读就重建任务或成果身份。

同一任务再次查询时，状态时间戳及文件摘要可能变化；公共归档接口会保留首次归档的状态快照和原成果版本。P 保存该接口确认的 `artifact_id`、`version_id` 和五个文件的大小、摘要，形成不可替换的 `receipt.json`。读回须与这份回执逐项一致，并继续核对原任务、请求、可信 seal、计划及实际成果内容。当前观察的摘要与归档快照的摘要可以不同；原观察和首次归档均保留，不覆盖成同一份状态。

旧记录没有 receipt.json 时，仍按原证据描述的状态摘要核对；需要接续已复用的早期快照时，先调用 `resume-archive`，由公共接口核实原版本并补存回执。回执保存中断时，可以从已保留的成功 archive-result 响应补存，或按原任务 resume；不能手工修改回执中的版本或摘要。

读回前先打开并校验既有回执。只有明确的文件不存在才使用旧记录兼容逻辑；失效或循环符号链接、其他非普通文件、权限错误、损坏内容和无效字段均拒绝，不能当作没有回执跳过。请求的 Artifact/Version 与已确认回执不符时也在启动 CLI 前拒绝，不生成读回目录或候选记录。修复本地读取问题后，仍沿原版本继续。

归档接续、恢复与读回都会核对原 archive.json：字段完整，run_id、attempt_id、producer、archive_id 和证据 operation_id 均须与当前运行匹配，证据描述也须符合约定格式。原记录身份错位或损坏时在启动 CLI 前拒绝，不能因为回执或成果内容看似正确就忽略。此检查只读取保留元数据，不要求原下载目录或文件仍然存在；已有归档仍可沿原版本接续。

候选只表示任务副本中的成果已经读回核对。要应用到开发工作区，仍须核对当前目标文件与基线；本适配器不自动应用、提交或合并 Git。

## 中断与失败接续

| 已知事实 | 接续方式 |
| --- | --- |
| 能力、来源或本地端点预检失败 | 留档、修正原因，再执行原 QUEUED 动作；没有消耗远端执行额度 |
| 已证明 CLI 没有启动 | 保留 not-started 记录，沿原尝试重新排队；不把超时当未启动 |
| 完整 CLI 响应已保存、P 响应落账失败 | `reconcile-action` 从原始 bytes 和端点核验记录补记原动作；不再调用 CLI |
| 控制进程被杀，原 CLI 或双流情况不明 | 保留原动作与缺口；沿原任务排队 query，必要时 cancel，不重提业务 |
| submit 返回 IO_UNCERTAIN 或查询 NOT_FOUND | 核对原任务；不能据此退还业务额度或生成新任务 |
| 原提交已完整返回错误，另有独立的原任务未接纳证明 | 用公共 `confirm_not_accepted` 保存绑定证明，保留原错误及调用额度；释放业务预留后，沿同一 operation ID 显式排队新提交。仅有错误或 NOT_FOUND 不满足条件，详见[接口](INTERFACE.md#调用与恢复语义) |
| 首次归档的证据描述或原状态预检失败 | 修正输入后再调用 archive-result；未固定错误描述，未启动归档 CLI |
| archive 完整返回 ERROR，且 CLI 已退出、双流 EOF 齐全 | 修正原因后可显式重调同输入的 `archive-result`；公共归档接口核对原 producer/operation 并复用已有写入，保留旧错误，不重跑业务 |
| archive 超时、输出超限、丢响应或收集不完整 | 先确认旧 CLI 已停止，再 `resume-archive` 恢复原 producer/operation；不能把未知结果当成未执行 |
| 归档 CLI 启动前失败 | 同原 archive-result 输入继续，保留每次未启动记录；未启动与完整 ERROR 共用最多 100 次接续额度 |
| 归档已成功，但 P 保存确认回执前中断 | 原成功 CLI 响应完整时，同输入 archive-result 复用原响应补存回执；也可按原任务 resume，不能改换版本或覆盖旧回执 |
| read 输出目录已发布但后续响应处理失败 | 保留原目录；确认旧 CLI 已停止后，按同一准确版本读到另一个新目录 |
| 输出摘要、来源或候选不符 | 保留原件，拒绝候选；不能手改收据使它变成成功 |

执行调用保留 intent、进程身份、原始 stdout/stderr、完整性摘要与退出结果。Local Hand 状态中的有限小数时间戳保存在原始响应中；核心仅保存绑定信息与规范化状态，原始响应 SHA-256 可定位原件。result 中的 `call_closed` 要求子进程退出和双流 EOF；它不表示远端业务退出。

归档操作使用独立进程锁，并检查旧 CLI 的进程身份。控制器锁释放而子进程仍在时拒绝重叠调用；确认 PID/启动身份已结束后，可以恢复原归档或在新目录读回。此时留下 `exit-observed`，明确 `output_complete=false`，不伪造丢失的输出或 EOF。若在启动与进程身份保存之间中断，自动核对仍不足，须保留缺口并核对原进程，不凭超时直接重发。

首次归档在形成公共恢复材料前失败时，resume 可能明确报错。已保存完整 ERROR 和 CLI 退出/双流 EOF 时，可以修正输入的可访问性后显式重调 archive-result；命令参数与原证据描述必须一致。每次调用最多启动一次 CLI，成功回执后再次调用只返回保留结果。公共归档接口先核对原任务与已提交记录，既有写入复用原版本；身份冲突或恢复材料损坏仍拒绝，不能通过换任务或归档目录绕开。

没有完整响应或进程停止证明时，仍保留未知状态，先沿原任务恢复；适配器不会自动重发或重建缺失的存储。首次归档与恢复归档都要求正确的 producer、operation_id、有效的 artifact_id/version_id 和 content_verified=true，之后仍需指定版本读回核对。

读取不存在的调用记录不会创建调用目录；保留文件若不是普通文件会立即拒绝，不等待命名管道的写入端。修正本地读取问题后可继续，不能把读取失败当作业务成功或旧调用的退出证明。

## 限额与验证范围

动作额度由核心持久预算控制；每次普通 CLI 默认 15 秒，归档 CLI 60 秒，双流各最多 512 KiB；超时和超限均保留原返回。证据清单最多 16 页，每次下载最多 1024 次分块调用，单个文件上限 16 MiB，workspace 核对成员上限 512 KiB。这些是单次上限；适配器不会自动删除调用日志或旧输出，操作者需为选定任务预留磁盘空间并有界重试。

合成服务下的集成测试实际启动所选 CLI、下载并校验封存文件、调用 Ledger 保存和读回，再核对候选/验收。它覆盖故障与协议边界；现场生产任务、原业务应用和 Ledger A3 的完整采纳仍需各自真实证据。
