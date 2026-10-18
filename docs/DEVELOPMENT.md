# 开发与接续记录

## 运行记录核心首批交付（2026-10-18）

Owner 在确认“先实现可安装、可运行、可保留并恢复任务记录的 P 核心”的建议后指示推进，并明确选择 MIT。本批实现沿用已建立的三层设计和直接提交 main 的方式；许可证仅用于本仓，不改变其他组件的许可。

当前提供 `0.1.0a1`：独立 Python 包、`production-plane` CLI、SQLite 运行/动作/历史/成功回执、执行互斥、取消屏障、计划修订与原任务恢复检查。可选知识保存准确版本和准入引用；学习候选只在本地保留，不假报发送或知识准入。精确能力与输入见[公共接口](INTERFACE.md)，可运行示例见[使用说明](USAGE.md)。

这是一批可独立安装和验证的运行记录核心，尚未完成完整生产闭环。`perform` 接收受信适配器回调；本批测试没有配置真实 Local Hand、Ledger 或知识服务。实际能力核对、完整协议验证、CLI 子进程及双流跟踪、证据取回、成果归档与读回验收仍属于后续适配工作；不能把本地回调观察当作现场证明。

### 关键实现与验证

- 原命令、业务状态、事件及成功回执同事务保存。重复创建或丢失回执使用原请求返回原记录；提交回执失败不能改报数据库回滚。
- 下发边界先记 `MAY_HAVE_RUN`，再调用适配器；无确定结果不自动重发。真实子进程在合成外部效果产生后退出，恢复仍保留原任务 ID，重复推进没有产生第二次效果。
- 执行互斥不包住状态写事务。单独进程的适配器等待期间，取消意图、状态查询和原任务取消回调仍可完成；第二个执行者无法重复推进。
- 确认本地未启动时保留失败并退还额度；提交前修正不消耗业务额度。未知提交、`NOT_FOUND`、取消回执和业务退出分别保存。
- 修订保留原计划、自动生成的任务身份及旧结果；修订前尚未排队动作的身份也不会丢失。已取消、未知或仍有未闭合调用的尝试不能通过新修订绕过。
- 必需知识缺席阻止提交，可选缺席保留原因；版本、内容摘要、准入来源及候选来源分别绑定，不代替实际知识服务验证。

初版实现期间的两个反例为“取消回调错误等待执行锁”和“提交前计划修正被拒绝”；相同回归用例已覆盖修正后的行为。另一个测试错误把带换行的原文与 JSON 转义后的字符串比较，已改为比较解码后的完整原计划，不改变历史保留要求。

验证使用 Python 3.12.14 / Linux、本地 SQLite、独立构建与安装环境；构建依赖固定为 setuptools 84.0.0、wheel 0.48.0。源码及独立 wheel 安装环境各通过 32 项测试；编译、wheel/sdist 构建、独立导入、合成记录示例及 CLI 接续检查通过。安装测试在源码目录之外执行，清除 PYTHONPATH，实际导入位于新环境的 site-packages；wheel 中的产品模块与受验源码逐文件一致，MIT 文件已包含。没有部署服务、重新执行历史业务或重做 A2。没有新增 CI 托管工作流。

### 接续

下一步按实施计划第 2 步完成真实执行适配：先核对已有运行入口和登记能力，将本批核心与真实调用的身份、超时、子进程退出及双流证据连接起来，再接 Ledger 归档和准确版本读回。应选择已有待处理任务并绑定真实来源与验收条件；本地核心测试不计为 A3 消费成功。K 的真实服务仍可后续接入。

## 建仓时状态（2026-10-18）

Owner 已创建公开仓库 `kongbu0621/control-production-plane`，初始提交为 `2c78806ac716b53d3836e541f8a4adaa6d4bf7f8`。本次整理 README、三层设计和维护入口；尚未实现产品 API、CLI、运行数据库或适配器，未执行现场任务。

本轮对话已确认继续使用 `control-production-plane` 名称：`control-` 表示生产控制，`infra-` 表示基础机制。前缀是职责分类，不是成熟度或权限声明。

方向为先推进生产控制的窄任务闭环，再根据真实需要接入知识能力。当前三份设计把首版具体方案列为提案，不能用仓库创建、文档提交或助手总结补造全部实现、部署和验收授权。Owner 后续具体指令按实际范围接续，不另设固定口令或独立批准提交。

## 核对过的来源

以下是本轮读到的固定源码，不是现场部署版本声明。维护源可能需要仓库访问权限，产品消费者不依赖这些私有设计资料运行。

| 仓库 | 固定提交 | 本轮用途 |
| --- | --- | --- |
| `code-driver-theory` | `e6c3814105977ad5b97a0bbd5a7b9c1bbf7489e2` | 当前路线、生产/学习双环、职责与命名 |
| `engineering-sop` | `9372247bdfa24f46694bf2435ef9917e155e166c` | 三层设计、建仓边界、风险相称验证、规则采用 |
| `infra-local-hand` | `f208f0fc174164f1b866499916759aad1aa22f84` | 公开任务接口、任务副本编辑、结果归档与读回 |
| `infra-artifact-ledger` | `58b08fa9cb188c48feec45592a0f775c7178fd7a` | 公共成果接口、版本与内容语义、A2 当前记录 |

理论定位入口：

- `mappings/artifact-knowledge-growth-integration-roadmap-v0.1.md`：能力地图与 P/K 推进关系。
- `mappings/repository-semantic-identity-naming-rule-v0.1.md`：`infra-` / `control-` 等职责分类。
- `mappings/code-driver-v2-modular-digital-production-architecture-v0.1.1.md`：控制面与其他责任边界。
- `theory/green/G-009-production-and-learning-dual-loop.md`：先取得生产闭环，逐步接入学习反馈。

公开依赖入口：

- [Local Hand 使用说明](https://github.com/kongbu0621/infra-local-hand/blob/f208f0fc174164f1b866499916759aad1aa22f84/docs/USAGE.md)。
- [Ledger 公共接口](https://github.com/kongbu0621/infra-artifact-ledger/blob/58b08fa9cb188c48feec45592a0f775c7178fd7a/docs/INTERFACE_PROFILE.md)。
- [Ledger 当前维护与验收记录入口](https://github.com/kongbu0621/infra-artifact-ledger/blob/58b08fa9cb188c48feec45592a0f775c7178fd7a/AGENTS.md)。

Ledger A2 在其记录配置内已完成。旧路线图的阶段快照不覆盖后续真实工程结果。本仓不重做 A2；将来完成的 Code Driver 实际消费只能贡献 A3 的一侧，独立 Generic Agent 的消费仍需另行证明。

## 本仓边界与形成依据

| 检查点 | 当前结论与待证事实 |
| --- | --- |
| 可复用职责 | 协调明确目标的执行、验证、修复、恢复和验收，独立于某个聊天界面和执行器 |
| 外部消费者 | 首个计划消费者是推进真实软件任务的调用方；CLI/应用/Agent 共享入口属于设计，尚无多个消费者实证 |
| 独立性 | 通过公共接口消费 Local Hand/Ledger，不导入旧原型或消费者私有代码；独立安装测试待实施 |
| 生命周期 | 控制策略和恢复语义可独立于成果存储、执行后端演化；实际兼容与发布成本仍须通过首版验证 |
| 耦合收益与成本 | 责任分离避免基础组件反向承载生产控制；同时增加版本组合、适配测试与恢复关联成本，首版先静态适配 |
| 当前选择 | Owner 已创建独立公开仓库；内部模块先留仓，不把每个端口继续拆仓 |
| 公开内容边界 | 通用产品文档与后续可披露代码；真实任务内容、机器配置、凭据与原始记录保持私有 |

仓库创建不是独立复用、生命周期或兼容性已经通过的实证。若实现显示多个模块仍需原子变化，先保留仓内模块边界，不为既有名称扩张项目。

## Git 与交付状态

当前本地 `main` 来自 GitHub `origin`，作为此轮协作与发布来源。尚未执行独立 Git Authority Admission，也没有验证新的权威仓或备份链；不能将普通 clone、GitHub 可访问或本次推送等同于这些结果。本轮不改 remotes、不操作 NAS Git 仓。

Owner 已要求直接提交推送主干、不另建分支；正常文档初始化沿用该方式。许可证尚未选择，仓库公开不等于已经按开源许可证授权使用；首次代码交付前明确此项。

## 初始文档验证（提交 a71f166）

已核对仓内相对链接、引用的固定提交和源码文件路径、文档末尾换行与空白差异，并检查公开内容中没有实际机器地址、路径、凭据或原始任务数据。需求、架构和计划对首版范围、未知结果、候选成果与应用边界的表述一致。

本次只有文档与忽略规则，不存在可编译的产品代码；没有运行产品测试，也不将文档检查报告成执行适配或现场验收通过。

## 2026-10-18 全仓复核

本轮根据 Owner 的全仓检查与直接修复指令，复核全部七个受版本控制文件。初始检查没有发现失效链接，但遗漏了以下设计边界；修正的是待实现方案，不把它们描述成已经发生的现场故障：

| 发现 | 修正及可定位依据 |
| --- | --- |
| 将双向知识接口笼统推迟到未来 | 首版补入可选知识读取与学习候选出口、未配置语义和来源绑定；理论固定基线 G-009 要求保留两个方向，不要求先建完整外环 |
| 混写准备接续与提交后查询 | 明确 `PREPARING` 原参数轮询 prepare、FAILED 的有界重试、READY 请求保存和过期处理；Local Hand `resident_service.py::ResidentService.prepare`、`resident.py::ResidentBroker.call` 的实际分支是核对依据 |
| 取消/重启保护安排在真实下发之后 | 把提交前的持久意图、取消屏障、子进程接续及额度预留放入第一步，后续补齐故障验证 |
| 包内自洽与本次候选身份未区分充分 | 补充运行到归档 source 的逐项匹配及验收到精确候选的绑定；归档器 `_load_version` 核对包内来源，不知道本控制器当前选择了哪个运行 |
| 未显式核对验证对其他源码的影响 | 对照 `workspace_jobs.py::finish_edit` 与归档器 `_source`，要求核对验证前后补丁及最终状态，不能只检查目标文件未变 |
| 本地重复请求、取消竞争和状态缺失语义不够具体 | 补充本地请求幂等、部署/主体绑定、准备中取消与先完成竞争、缺记录不重建、预算跨重启保留 |

上述 Local Hand 源码均位于已记录的 `f208f0fc174164f1b866499916759aad1aa22f84`，具体路径为 `tools/admin/local_hand_system_manager/resident_service.py`、`tools/local_hand_mcp/resident.py`、`tools/local_hand_jobs/artifact_archive.py` 和 `tools/local_hand_jobs/workspace_jobs.py`。这是源码与文档的语义核对，没有调用线上准备、提交、取消或归档入口。

本轮验收采用全文件复读、仓内链接及固定引用核对、Markdown 结构与空白检查，并将正常、失败、丢响应、重复、取消竞争、错误版本、验证污染及未配置知识等边界逐项映射到实施计划。程序编译、故障注入和安装态验证仍待实现后执行，不把矩阵里的预期结果标成实测通过。

## 2026-10-18 第二轮全仓复核

复核起点为 `4632865e466bc68ee198f7c88a1303ebd0a165e2`，全部七个文件重新检查。本轮修正三组会影响实现的接续问题，没有新增产品模块：

- **取消可达性和在途提交**：区分执行者占用与短时状态锁，不在写事务中等待远端响应；取消先于接纳返回 `NOT_FOUND` 时保留意图，原任务出现后继续取消。旧调用退出/EOF 不明不阻断原任务的有界只读查询。
- **读回的本地副作用**：`read_version` 先发布新目录再返回结果，发布后丢回执时同路径重试会冲突。方案改为保留原输出，在预算内用新目录读回同一版本并重新核对，不仅凭目录存在认定成功。
- **准备修订与不可变历史**：明确接纳前失败或过期请求经原服务允许后可沿同一 UUID 重新准备，新 READY 请求另存修订；原已发送请求保持历史，不把保留历史误解成禁止合法接续。

依据仍是固定 Local Hand `f208f0fc174164f1b866499916759aad1aa22f84`：`ResidentBroker.call` 在 IDLE 取消时返回未找到，`Broker.cancel` 只操作已经存在的原任务，`artifact_archive.read_version/_publish_files` 先发布目录后返回。没有更改 Local Hand 源码或调用线上任务。

针对新疑问，运行了该依赖既有 `tests/test_local_hand_resident.py` 中的六项单元测试，全部通过、无跳过：

| 测试类 | 方法 |
| --- | --- |
| `ResidentTests` | `test_cancel_before_submission_or_during_preparation_does_not_launch` |
| `ResidentTests` | `test_cancel_response_loss_never_redelivers_or_restarts_and_status_remains_usable` |
| `ResidentTests` | `test_one_transient_read_failure_does_not_disable_future_queries` |
| `PreparationTests` | `test_successful_preparation_retry_polls_preserve_the_ready_request` |
| `PreparationTests` | `test_stale_unsubmitted_request_can_still_be_prepared_for_the_same_uuid` |
| `PreparationTests` | `test_explicit_retry_replaces_only_a_proved_uncommitted_execution_failure` |

这些测试使用模拟执行端和临时记录，仅核实所依赖的分支语义，不是本控制组件已实现或现场通过的证据。读回发布边界采用源码及既有测试用例核对，未重新运行归档集成测试。仓内链接、固定引用、Markdown 结构与提交差异检查通过；本仓仍无编译目标，新增取消竞态、锁行为和读回故障用例列入实施计划，尚未作新组件实测。

## 下一步

按[实施计划](IMPLEMENTATION_PLAN.md)从最小运行记录和公共入口开始。首次实际任务前核对现有部署、选择已登记的真实目标和具体预算；未知部署细节先查现有配置和记录，不要求 Owner 重找已有机器、共享和凭据。

后续每次交付据实更新已实现能力、验证来源和剩余缺口。不要将本节未实施的方案写成运行事实，也不要为普通修复建立新的历史审批链。
