# 开发与接续记录

## 当前状态（2026-10-18）

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

## 下一步

按[实施计划](IMPLEMENTATION_PLAN.md)从最小运行记录和公共入口开始。首次实际任务前核对现有部署、选择已登记的真实目标和具体预算；未知部署细节先查现有配置和记录，不要求 Owner 重找已有机器、共享和凭据。

后续每次交付据实更新已实现能力、验证来源和剩余缺口。不要将本节未实施的方案写成运行事实，也不要为普通修复建立新的历史审批链。
