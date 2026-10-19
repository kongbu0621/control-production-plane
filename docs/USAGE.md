# 安装与本地记录使用

当前交付运行记录核心和可选执行/成果适配器。本页先介绍不会下发任务的本地记录示例；连接已有服务、归档、读回与验收见[适配器使用](ADAPTERS.md)。

## 构建与独立安装

支持 Linux / Python 3.12。运行记录核心仅依赖标准库；使用适配器时另行安装所选 Local Hand 和 Ledger wheel。先从本仓取得源码，在源码目录创建独立构建环境：

```sh
python3.12 -m venv .build-venv
.build-venv/bin/python -m pip install setuptools==84.0.0 wheel==0.48.0
.build-venv/bin/python -m pip wheel --no-build-isolation --no-deps . -w dist
python3.12 -m venv /absolute/private/production-runtime
/absolute/private/production-runtime/bin/python -m pip install --no-index --no-deps dist/control_production_plane-0.1.0a6-py3-none-any.whl
```

有离线 wheel 缓存时，为构建依赖安装加 `--no-index --find-links /path/to/verified-wheelhouse`。不要复用其他项目的运行环境，也不要把运行数据放进源码目录。首次初始化只接受不存在的存储目录，其父目录需要已存在。

## 完成本地记录闭环

把以下运行环境路径换成实际安装位置，示例文件位于源码的 `examples/`：

```sh
/absolute/private/production-runtime/bin/python examples/local_records.py /absolute/private/new-production-records
/absolute/private/production-runtime/bin/production-plane --store /absolute/private/new-production-records show --run-id example
/absolute/private/production-runtime/bin/production-plane --store /absolute/private/new-production-records recover --run-id example
/absolute/private/production-runtime/bin/production-plane --store /absolute/private/new-production-records events --run-id example
```

预期：创建一份目标与计划，相同请求重放返回原回执；取消意图保存后，重新打开仍返回同一个任务身份和 `NO_NEW_SUBMISSION`。`external_calls_made=0` 表示本次 recover 没有外部调用，不是对历史是否执行的证明。示例再次运行相同目录会拒绝覆盖；已有记录用 `show` / `recover` 查看。

普通运行使用 `init` 创建新存储，再通过 `write --request FILE` 保存 [公共接口](INTERFACE.md)规定的请求。请求文件可以包含源码和目标内容，应放在私有目录。原请求 ID、运行目录、绑定和未决记录都要保留。

修订后可以用 `show --run-id ID --revision N` 查看原计划及原尝试状态；不带 `--revision` 返回当前修订。

## 失败后继续

- 写入返回失败或回执丢失：保存原请求，先查看原运行；同 ID 同内容重试查询原提交，不另起任务。
- 读取报错：修正路径、权限或临时 I/O 原因后继续读取；缺失数据库不自动重建。
- `PENDING`：先读取 `recover` 返回的原任务与未闭合调用；未知提交不能换 ID 重做。
- `BUSY`：另一个执行者占用执行权；仍可查询和记录取消意图。
- `LIMIT`：预算不会因重启清零。保留当前状态和历史，禁止通过删除记录绕过。

## 验证本仓

从源码运行测试：

```sh
PYTHONPATH=src .build-venv/bin/python -m unittest discover -s tests -v
.build-venv/bin/python -m compileall -q src examples tests
```

安装态验证应离开源码工作目录并清除 `PYTHONPATH`，使用独立 runtime 的解释器，确认 `control_production_plane.__file__` 位于该环境的 `site-packages`，再运行本仓测试和示例。核心测试覆盖实际 SQLite 事务、独立进程、中断、竞争、CLI 短写和回执输出失败。`test_adapters` 在安装所选 Local Hand/Ledger 包后，使用合成 Unix 服务和真实 CLI、证据校验、Ledger 存储验证集成；缺少依赖时明确跳过这些集成测试。不得将合成任务结果报告成现场任务或 A3 完成。
