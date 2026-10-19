# control-production-plane

可独立复用的生产控制组件，协调目标、计划、执行、验证与恢复，通过明确接口组合执行器和基础能力。

它要解决的问题是：**一项任务交给执行器之后，如何持续推进，知道做到了哪一步，处理失败，并拿到能按原目标验收的成果。**

**当前版本：`0.1.0a5` alpha · Linux / Python 3.12 · [MIT 许可](LICENSE)。** 已有运行记录核心、Local Hand 公共 CLI 适配、证据下载、Ledger 归档与指定版本读回，以及可选 K 知识采用、结果反馈和明确学习候选的可靠投递。适配链路已通过合成验证，并在原部署完成一次真实 README 候选的执行、归档、指定版本读回与明确验收；P/K 已完成一次真实知识采用、候选验收与结果回流；另已通过五个安装态合成组合恢复窗口，以及已有真实试点的只读恢复对账；真实副本切换、其他故障边界和生产资格仍待验证。当前已实现入口见[开始使用](#开始使用)。

## 它负责什么

| 使用者的问题 | 本组件的职责 |
| --- | --- |
| 要完成什么，怎样算完成？ | 保存明确目标、约束和验收条件，推进过程中保留原始要求 |
| 交给谁做，使用哪些输入？ | 选择已配置的执行方式，绑定来源版本、输入、验证方式和资源限制 |
| 执行失败后怎么办？ | 根据真实结果协调修复或结束；新尝试关联旧尝试，保留失败记录 |
| 程序退出、连接中断后怎么办？ | 恢复原运行记录，先查询原任务，避免重复执行业务 |
| 最终交付的究竟是什么？ | 按准确版本取回成果、核对内容，关联验证结果和验收决定 |

首版面向软件任务：调用方提供一个明确的单文件修改方案，在已登记项目的任务副本中执行固定验证，保存候选成果，再按指定版本取回供审查和验收。需要修复时，调用方提供修订方案，由控制组件在原目标和预算内继续协调。

## 与其他组件的关系

<a id="与-local-handledger-的关系"></a>

下表说明组件职责。P 已提供 Local Hand 和 Ledger 的静态适配器；实际可执行范围取决于部署开放的项目与验证。P 还可经 K 公共库查询知识、登记准确版本的采用，并回流原任务结果。

| 组件 | 主要职责与本仓关系 |
| --- | --- |
| **control-production-plane（P）** | 协调目标、计划、执行、验证、修复与接续，关联成果及验收决定 |
| [infra-local-hand](https://github.com/kongbu0621/infra-local-hand) | 执行后端：P 通过其公共入口准备、提交、查询和取消任务；Local Hand 执行获准任务并返回原状态与封存证据 |
| [infra-artifact-ledger](https://github.com/kongbu0621/infra-artifact-ledger) | 成果存储：P 经现有 `local-hand-archive` 保存成功封存的候选成果，按准确版本读回供审查；Ledger 保存内容与来源 |
| [domain-operational-knowledge-core（K）](https://github.com/kongbu0621/domain-operational-knowledge-core) | 可选知识组件：提供已准入的准确知识版本；P 登记采用、回流使用反馈并投递明确学习候选，知识审查和状态管理留在 K |

知识接入是可选能力。任务未把知识列为必需输入时，可以独立推进，并记录未配置或降级的原因；任务若明确要求必需知识，未配置、无匹配或查询失败时，应阻止依赖它的新执行。不能伪造知识查询或回流成功。

`control-` 表示生产控制职责；`infra-` 是 infrastructure 的缩写，表示跨项目通用的基础机制。名称按主要职责分类，可独立复用的组件不一定都属于 `infra-`。

例如，一次修改经过以下过程：

1. 调用方给出目标、原文件来源、修改方案和验收条件。
2. 控制组件绑定执行能力，交由 Local Hand 在任务副本中修改和验证。
3. 控制组件查询同一任务，收集结果；满足归档条件的成功封存结果保存到 Ledger。
4. 后续审查按返回的成果 ID 和版本 ID 重新取回内容，核对来源、补丁和验证结果。
5. 验收者依据原目标给出决定，调用方拿到明确版本的候选成果和验收记录，决定下一步如何使用。

固定验证通过是验收依据之一。首版的交付终点是**有明确版本、验证结果和验收决定的候选成果**；应用补丁到目标工作区、提交或合并 Git 属于后续操作，应分别核验和记录。

## 首版边界

- 单用户、单机、串行推进一项任务，使用静态配置的适配器。
- 首个执行适配面向 Local Hand 已登记的 `workspace.edit` 能力；不承诺任意项目、任意命令或任意执行器可用。
- 修复方案由调用方提供。模型生成方案、多执行器选择、并行调度和图形界面留待真实需要明确后扩展。
- 控制组件消费执行观察和验收决定；不会把“收到响应”“退出码为零”或“成果已保存”自动等同于目标完成。
- 可选知识适配器支持每次尝试采用一个准确版本，保留采用意图、回执及结果反馈；知识缺席时能否继续，按任务预先确定的条件判断。调用方明确提供学习候选后，可发布原运行材料并投递到 K，保存接收回执；候选形成与准入判断仍由调用方和审查者负责。

## 开始使用

从源码构建并安装 wheel；尚未发布 PyPI 包。完整步骤见[安装与使用](docs/USAGE.md)。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install setuptools==84.0.0 wheel==0.48.0
.venv/bin/python -m pip wheel --no-build-isolation --no-deps . -w dist
.venv/bin/python -m pip install --no-deps dist/control_production_plane-0.1.0a5-py3-none-any.whl
```

首次体验可运行[本地记录示例](examples/local_records.py)，使用一个不存在的私有目录：

```sh
.venv/bin/python examples/local_records.py /absolute/private/new-production-records
.venv/bin/production-plane --store /absolute/private/new-production-records show --run-id example
.venv/bin/production-plane --store /absolute/private/new-production-records recover --run-id example
```

示例创建合成目标和计划，保存取消意图，再打开同一记录核对接续点；不会提交远程任务。`recover` 返回当前记录和下一步判断依据，本身不启动执行或重放动作。

| 当前公共入口 | 用途 |
| --- | --- |
| `production-plane init` | 在新目录初始化私有运行记录 |
| `production-plane write --request FILE` | 通过带请求 ID 的 JSON 命令保存本地变更 |
| `production-plane show --run-id ID` | 查看当前目标、计划、尝试、取消意图和预算 |
| `production-plane recover --run-id ID` | 读取原任务身份、未决调用和接续建议 |
| `production-plane events --run-id ID` | 分页读取保留的原始命令历史 |
| `production-plane inspect-executor` | 核对配置的 Local Hand 能力与身份 |
| `production-plane execute-action --action-id ID` | 执行已排队的准备、提交、查询或取消动作 |
| `production-plane reconcile-action --action-id ID` | 从保留的完整 CLI 回应补记原动作，不重发 |
| `production-plane collect-evidence --action-id ID` | 按原状态中的可信封存标识完整下载证据 |
| `production-plane archive-result --run-id ID` | 将成功封存的结果交给公共归档入口保存 |
| `production-plane resume-archive --run-id ID` | 核对旧 CLI 停止后，恢复原任务的归档 |
| `production-plane read-artifact --run-id ID` | 按准确成果/版本读回，校验实际内容并记录候选 |
| `write` 的 `candidate` / `decide` 操作 | 保存已核对的候选与逐项验收决定 |
| `production-plane query-knowledge` / `select-knowledge` | 查询匹配知识或核对指定版本的内容、条件与审查引用 |
| `production-plane adopt-knowledge` | 按原尝试登记选定版本，保存原使用回执；新提交还须重新核对知识当前状态 |
| `production-plane knowledge-feedback` / `resume-knowledge` | 保存原任务结果反馈，或沿原请求恢复知识调用 |
| `production-plane show-learning` / `deliver-learning` / `resume-learning` | 读取本地学习候选、投递到 K，或核对原请求并接续；接收不等于准入 |
| Python `initialize` / `open_store` | 显式创建或打开控制存储 |
| Python `ControlStore.perform` | 为受信适配器保留调用意图、互斥和响应；需调用方提供适配器 |

命令均需 `--store DIRECTORY`。已有目录、缺失或损坏的数据库不会被自动初始化覆盖。精确字段、调用闭合责任及错误处理见[公共接口](docs/INTERFACE.md)。

执行和归档适配器需要在独立环境安装所选 Local Hand/Ledger 包，并使用显式的私有配置；配置与操作顺序见[执行、归档与验收](docs/ADAPTERS.md)。本组件按保存的任务方案逐步调用，不自动生成修复方案，也不自动应用补丁或提交 Git。

知识适配器的安装、配置、采用顺序及恢复方法见[接入运行知识](docs/KNOWLEDGE.md)。查询不会登记采用；采用回执也不代表任务已执行。选用知识的任务提交时提供 `--knowledge-adapter`，重新核对原版本仍有效、适用条件和材料内容；已撤回或当前无法核对时停止新提交，实际结果单独记录。

## 文档

- [需求](docs/REQUIREMENTS.md)：为谁做、首版做什么、如何验收。
- [架构](docs/ARCHITECTURE.md)：职责划分、接口方向、持久化与恢复。
- [实施计划](docs/IMPLEMENTATION_PLAN.md)：实现顺序和与风险对应的验证。
- [公共接口](docs/INTERFACE.md)：已有库、CLI、JSON 命令和持久化语义。
- [安装与使用](docs/USAGE.md)：构建、独立安装、本地示例及恢复操作。
- [组合恢复核对](docs/COMBINED_RECOVERY.md)：P/K/Ledger 的恢复点、回执、材料和写入者边界，以及可重复的隔离演练。

开发过程、阶段记录和任务接续见[开发与接续记录](docs/DEVELOPMENT.md)；参与维护从 [AGENTS.md](AGENTS.md) 开始。
