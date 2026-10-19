# 接入运行知识

P 可以通过独立 K 组件查询已审查知识、采用准确版本，再把任务结果回流。首版每次尝试采用一个版本；未配置时原来的可选/必需知识规则继续有效。它不会自动审查知识、自动推理修改方案或把成功任务自动转成知识。

## 安装与配置

在 P 的独立 Python 3.12 环境安装已核对的 `domain-operational-knowledge-core==0.1.0a2` 和 `infra-artifact-ledger==0.2.0a1` wheel。P 提供 `knowledge` 可选依赖组；包尚未发布到 PyPI，按各仓说明构建后从本地 wheel 安装。不使用 K 的调用者不必安装它。

K 必须是已经初始化并完成生命周期升级的原存储。P 不替用户创建、迁移或覆盖 K 数据。配置文件应位于调用者私有目录，权限 `0600`：

```json
{
  "store_path": "/absolute/private/knowledge",
  "store_id": "从原 K 成功回执取得",
  "anchor": {
    "scope": "example:reviewer",
    "operation": "review",
    "request_id": "request:review",
    "fingerprint": "原回执中的64位请求摘要"
  },
  "scope": "example:production"
}
```

这是字段示意，必须换成真实原回执。`anchor` 可引用既有审查等成功操作。P 在任何 K 写入前调用 `get_operation` 核对其存储身份与摘要；路径相同不代表还是原数据库。锚点只证明所连数据身份，不授予权限，也不要求原版本永远处于 ACTIVE。P 不读取 K 私有表或导入 K 私有实现。

## 从查询到结果反馈

所有命令需 `production-plane --store P目录`，下列命令另需 `--adapter 私有配置文件`。

| 命令 | 参数与作用 |
| --- | --- |
| `query-knowledge` | `--kind KIND --context FILE [--after ID --limit 10]`，分页查询适用的有效知识；不登记采用 |
| `select-knowledge` | `--version-id ID --context FILE`，按准确版本核对内容、条件和来源，返回 `item`、`content`、`conditions` |
| `adopt-knowledge` | `--run-id ID --context FILE`，登记当前计划选用的版本；保存原请求后调用 K，核对成功回执并落 P 账 |
| `knowledge-feedback` | `--run-id ID --revision N --notes TEXT`，保存该修订原尝试的观察、验收、知识使用和说明；不会启动业务 |
| `resume-knowledge` | `--run-id ID --call-id ID`，先查原 K 回执，再按原请求恢复；不重跑生产任务 |

`context` 是 K 公共合同的 JSON 对象，使用私有 `0600` 文件。库入口为 `KnowledgeAdapter(store, config)`；方法依次为 `query(kind, context, *, after='', limit=10)`、`selection(version_id, context)`、`adopt(run_id, context)`、`feedback(run_id, revision, notes)`、`resume(run_id, call_id)`。上下文先按 K 的公开约束本地校验：至多 16 项，小写字段名，每个值为非空且不含 NUL 的字符串、UTF-8 至多 256 字节；不合规时不保存调用意图，修正后可继续同一尝试。K 的公共 `KnowledgeError` 会原样传给库调用方；CLI 将依赖异常保留为未知结果并提示核对，不能把错误解释为无匹配。

1. 查询并阅读实际知识内容，明确它怎样影响计划或验收。查询没有匹配、查询失败和未配置是不同事实；在计划中分别记录 `NO_MATCH`、`QUERY_FAILED` 或 `UNCONFIGURED` 及原因。
2. 创建或修订 P 计划，把 `selection.item` 原样放入 `plan.knowledge.items`，设 `status=SELECTED`，在 `reason` 写清具体使用方式。这里仅保存选择，不表示 K 已登记。条件正文超过 P `scope` 的 2048 字节时，该字段使用 `conditions-sha256:` 加完整条件的规范化 UTF-8 JSON 摘要；`selection.conditions` 仍返回完整条件，采用时核对同一摘要和完整上下文。
3. 在业务提交前调用 `adopt`。K 的原 `start_use` 回执确认是提交选定知识任务的必要条件；提交前还须通过下一步的当前状态核对。`required=false` 也不能绕过已经声明选定版本的真实性检查。知识在查询后被退役或替换，新采用会被拒绝；修订后的新尝试需要自己的使用记录。
4. 提交时给 `execute-action` 增加 `--knowledge-adapter 私有K配置文件`；库调用为 `LocalHandAdapter.perform(action_id, knowledge=KnowledgeAdapter(store, config))`，或 `store.perform(..., knowledge=adapter)`。P 在执行器预检后重新检查原使用回执、原版本当前 ACTIVE 状态、原上下文适用条件、内容/审查绑定和材料字节。即使 `required=false`，已选定的版本也必须通过；不通过时保持 QUEUED、不调用业务提交、不消耗执行额度。没有选用知识的任务沿用原规则。随后通过原归档适配器推进，验收按原目标单独进行。
5. 对该修订调用 `feedback`。事实摘要由 P 原记录生成，包含任务 ID、计划摘要、执行观察、候选与验收决定；`notes` 是调用者的解释。`SUCCEEDED` 只表示记录的执行结果，不能代替 `ACCEPT`；未知结果仍为 `UNKNOWN`。反馈仅保存材料，不自动准入新知识。反馈快照的 UTF-8 JSON 或 P 规范化 ASCII 转义 JSON 任一超过 64 KiB 时，会采用 `snapshot_format=bounded-references-v1`：保留执行观察、验收决定、检查通过标志与证据引用，以每项条件的 UTF-8 SHA-256 替代长条件正文，并记录原候选/决定的 P 规范化摘要及完整快照 UTF-8 JSON 摘要。原正文仍由 P 账本保存，反馈不截断、也不删除检查项。较小记录沿用原完整格式。P 整条运行记录仍受 256 KiB 限制；摘要格式不扩大该限制，也不保证任意历史数量都能继续追加。已保存意图的旧完整格式反馈仍按原字节恢复，不重新生成。

K 的 `start_use` 表示为原任务登记采用；后续取消或未提交的尝试仍保留该记录，实际是否执行在反馈中说明。登记不是执行许可，不改变 Local Hand 的权限或预算。

## 中断与边界

P 先事务保存调用意图，再调用 K，最后保存 K 回执。三者不是一个跨库事务。只持有推进租约，K 调用期间不占 P 数据库写事务，因此仍可记录取消。

新意图同时在 P 的 256 KiB 运行记录上限内预留成功回执与取消记录所需的容量，包括合法取消理由的最坏 JSON 转义长度。待确认期间其他运行写入也须保留这些空间；不足时在 K 写入前返回 `LIMIT`，本地事务回滚，原任务和历史保留。预留使用内存中的长度/结构上界检查，不把占位内容写成真实回执或取消记录；成功确认后解除相应预留。此处只保护 JSON 记录容量，不承诺磁盘空间或跨库事务成功，所有写入该 P 存储的进程需使用支持预留的版本。

- K 写入成功但响应丢失：按原 scope、操作和请求 ID 查回执；不会创建新使用或重放生产。
- P 回执落账失败：原意图保留，恢复补记同一回执；普通本地错误修复后可以继续。
- 查询或写入报错：原失败返回应由调用方保留。`recover.pending_knowledge_calls` 和 `run.knowledge_calls` 提供原请求，不能改参数重用同一 ID。
- K 原回执查询结束后，重新读取 P 最新状态；此时原尝试已取消或修订，恢复不再发起新的采用；如果 K 已成功，则允许补记原回执。旧 pending 记录不会阻止修订后的独立尝试。该检查不是跨库锁：若取消发生在 K 写调用已经开始之后，保留实际发生的采用回执，P 的事务取消屏障继续禁止业务提交，反馈如实记录未执行。
- 已在 P 确认的 K 回执消失：拒绝静默重建，先核对 K 恢复状态。
- 旧版未预留容量的 pending：先查 K 原回执；已有成功结果就补记它，尚无成功回执时先事务补记容量预留，再按原请求接续。已取消或被替换的旧采用不新增预留或写入；旧记录自身已满、不能保存回执时保留待确认状态，不能清空历史或换 ID 重写 K。
- 迟到反馈：仍绑定原修订、原任务和原使用，不套用到新尝试。初始反馈一旦保存意图，其事实快照和说明固定；重试不以最新状态覆盖旧快照。

首版支持一次初始反馈；需要后来纠正时使用 K 的正式 `correct_feedback` 合同并保留原回执，本版 P 尚无修正适配器。`learning_candidate` 仍只保存本地待审候选，不声称已投递。P/K/Ledger 组合备份恢复、跨副本写实例隔离及任意多个知识版本组合不在本次完成范围。

## 执行前的当前状态检查（0.1.0a4）

`KnowledgeAdapter.check_execution(state)` 只经 K 的 `get_operation` / `get` 公共接口读取，核对原存储、作用域、使用回执及其原任务/上下文，再读取计划固定的准确版本。这个方法返回观察，不返回可供以后使用的执行许可。`perform` 每次新提交都在执行器预检后重新调用它，随后在短事务中重核取消、修订、请求及预算；调用 K 时不持有 P 写事务。

成功观察随同提交保存在 `action.knowledge_check`，包含 K 身份、原使用和回执摘要、版本及内容/审查绑定、上下文摘要、观察时间和已核对材料引用。它只说明当次检查结果；重启后不据此重新提交。直接调用底层 JSON `dispatch` 不能用旧采用回执放行 SELECTED 任务，需通过 `perform`。内部检查参数不是公共协议；受信 Python 进程本身仍不构成恶意调用者隔离边界。

提交事务还会检查动作记录的字节容量：原请求、当次知识检查与计划声明的 `max_response_bytes` 必须能共同存入 256 KiB 动作记录，并按较长的 `call_closed=false` 编码计算。不足时在调用业务前返回 `LIMIT`，事务回滚，动作保持 QUEUED，既有采用回执和预算不变。不能等响应回来后再发现检查记录挤占了它的空间，也不把检查或响应截断。新运行应声明与实际响应需求相称的预算；原运行不能静默改预算或清空历史，显式修订计划后仍需重新检查。这项容量检查针对规范化 JSON 字节，不承诺磁盘可用或取消其他输入结构限制。

原使用已经登记但版本随后 RETIRED 或 SUPERSEDED 时，阻止依赖它的新提交；不会自动换用新版本、改写原使用或更改已经执行的结果。需要改变输入时显式修订计划，并为新尝试重新登记采用。读取失败返回 `KNOWLEDGE_UNAVAILABLE`，版本不再适用返回 `NOT_READY`，身份矛盾返回 `IDENTITY_CONFLICT`；失败代码作为 `preflight_failure` 事件保留，原动作仍可在原因修复后重新核对。业务可能已经下发的动作继续查原任务，不能因重新通过知识检查重发。

查询、取消、旧响应补记和迟到反馈不要求旧版本仍 ACTIVE；历史回执恢复与新执行资格分别处理。CLI 对 query/cancel 不读取 `--knowledge-adapter`，K 故障不阻断这些已有任务的接续。

这是 P 提交前的有限检查窗口：K 状态读取、Ledger 内容读取、P 提交记录与执行器接纳不是一个跨组件原子事务，检查后发生的撤回不构成自动取消。上下文由调用方声明，组件不自动探测环境。当前 K 的 ACTIVE/条件匹配不代表已实现未来的有效期、暂停或权限治理合同。

一致但过旧的 K 副本可能仍显示 ACTIVE；原锚点和当前读取不能自行证明它没有遗漏快照后的退休。本能力不自动启用恢复副本。组合恢复仍需由组装流程核对恢复点、已知后续决定和未知范围，停止或隔离原写入者后显式选择当前实例；P、K、Ledger 各自保留其状态责任。

五个安装态合成组合恢复窗口及操作顺序见[组合恢复核对](COMBINED_RECOVERY.md)。旧快照遗漏后来退休和不同副本的锁相互独立均已实际复现；演练中的后续回执对账和写入者检查属于协调步骤，不是本库新增的自动副本启用接口。
