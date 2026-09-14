# R2 前两阶段：RPC 边界与任务工作区

日期：2026-09-08。状态：本地实现与验证；未部署、未签署生产验收。

本次仅落实 RPC/授权入口与任务工作区，不引入 Luna、双模型、第二 Runtime、
Core Agent、长期记忆或新的平台操作。R3.1 搜索开关及人格 prompt 不变。

## 第一阶段：执行凭证与纯数据 RPC

- `/ext/rpc_exec` 的请求和返回值改为 JSON。无 pickle 兼容回退；请求最大 1 MiB、
  嵌套深度 32、节点数 50,000；拒绝重复字段、非有限浮点数和未知请求字段。
- 协议支持 null、bool、整数、有限浮点数、字符串、列表、字符串键字典。
  Python tuple 在发送端按 JSON 数组处理；bytes、Path、自定义对象需插件显式转换
  为纯数据或文件引用，不在主进程恢复任意 Python 对象。
- 每次沙盒执行签发独立随机凭证；服务端只保存摘要索引，并绑定可信 AgentCtx、
  当前允许方法的函数实例及失效时间。模型不能通过 query/body 自报会话、用户或目录。
  凭证不落入 manifest，执行结束、异常、取消及服务关闭均撤销，重启后不恢复。
- 调用端等待串行锁后重新收集当前上下文可用方法。方法停用、移除、替换、同名冲突、
  凭证撤销、预算耗尽均拒绝。一次任务的 64 次调用预算跨调试轮次保留，不随新凭证重置。
- 常见 `chat_key` / `from_chat_key` / `target_chat_key` 参数限制在当前会话。
  这是明确的收紧；跨会话操作不能沿用旧的任意目标调用方式。
- TOOL/AGENT/BEHAVIOR/MULTIMODAL_AGENT 的结果仍交给现有 Python 循环。
  失败不再被记成成功的系统结果；Agent 续写必须有服务端记录的成功 Agent 调用。

范围限制：方法集检查不是所有插件的完整资源级 authority。任意字典中的目标、
URL 出口、管理动作及收费服务仍需逐插件梳理并接入相应权威服务，不能将本阶段
称为“所有外部操作均已由 Core 授权”。已开始的外部调用也不能靠撤销凭证倒退。

## 第二阶段：任务文件与执行隔离

`run_agent` 为每次任务生成随机 task ID，同一执行循环的重试复用该 ID。
目录由服务端生成；手动执行不传任务 ID 时分配新工作区。

| 容器路径 | 内容与权限 |
| --- | --- |
| `/app/shared` | 本任务工作文件，可写；预建 `work/`、`out/` |
| `/app/uploads` | 当前会话上传目录，只读；第一版不是按附件逐文件选取 |
| `/app/packages`、`/app/.pip_cache` | 本任务独立依赖层，不再全局共享可写包 |
| `/app/control` | 本轮脚本与生成的调用器，只读 |
| `/app/diagnostics` | 宿主保存的有界原始输出，只读，最多保留最近 8 轮 |
| `/app/broker` | 离线模式下的专属 Unix RPC socket，只读挂载目录 |

manifest 保存在不挂入沙盒的 `.r2-state/<task_id>/`，记录会话、执行序号、
剩余调用预算、状态和最近活动时间。并行任务不能共用同一工作区；不同任务不再
互相终止容器。活跃执行保护清理；30 分钟不活动后仅清理本实现登记的任务目录。
旧的会话沙盒、历史依赖目录、上传文件及其他目录不在新清理器的删除范围。

启动时持有数据目录的排他文件锁，不支持多个 Runtime 进程共享该数据目录。
仅清理带本数据目录 owner 标签的遗留 R2 容器，再将未完成任务标成 interrupted。
不会自动重放代码、RPC 或消息发送；没有增加跨进程任务恢复 UI/API。
定时清理会在重启后继续工作，不依赖重启前的内存计时任务。

容器使用本地镜像解析出的 immutable image ID，不隐式拉取新镜像。固定 nobody UID、
只读 rootfs、全部 capability 移除、no-new-privileges、128 PID、1 CPU、512 MiB 内存
及同额 MemorySwap；`/tmp` 为 64 MiB tmpfs。没有挂载 Docker socket、Core 密钥或数据库凭据。

stdout/stderr 流式读取，超过 256 KiB 停止容器，Docker 日志额外设置 1 MiB 单文件上限。
执行状态读取 Docker 实际退出码，不再通过 stdout 中的结束标记判定。
短诊断被截断时会给出 `/app/diagnostics/execution-N.txt`，可在同一任务内继续读取。

文件导出使用目录描述符逐级 `O_NOFOLLOW` 打开，拒绝路径穿越、符号链接、硬链接、
目录和 FIFO 等特殊文件，校验写入期间变化，复制到任务独立的可信 staging。
`FileSystem.get_file` 和现有基础文件发送入口使用该快照；没有新建发送能力或投递协议。
普通路径转换仍是转换函数，不能替代安全打开；绕过这些入口的第三方插件须另行审计。

## 网络与磁盘：不可省略的验收限制

`SANDBOX_OFFLINE_MODE` 默认 **false**，不会因本次代码改动自动切断生产任务网络。
启用后使用 Docker `NetworkMode=none`，移除 host-gateway，仅通过本任务专用 Unix socket
调用获准方法；它不是通用 HTTP 代理。没有公网时，动态 pip 安装会失败；需事先核验
常用计算依赖与插件兼容性。网络隔离失败不会自动降级到 bridge。

兼容模式仍为 bridge，不能宣称阻止了沙盒直接访问网络。即使启用离线模式，可信插件
仍可能在宿主访问外部服务；插件方法授权/费用策略与容器出口是不同边界。

磁盘实行 64 MiB **单文件硬限制**，工作文件和依赖层的 64 MiB 总量、10,000 个条目、
64 层嵌套由宿主每 250 ms 监测，超额或无法检查时撤销凭证并停止执行。
监测是软总量限制，不是文件系统硬配额，存在轮询间隔内的突发写入。
可信导出区另有 64 MiB 总量限制。不能据此签署恶意无限写入场景的生产验收；
严格磁盘上界需部署侧文件系统配额或真正有硬容量限制的持久存储方案。

资源限制参考 [Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)，
离线网络语义参考 [Docker none network](https://docs.docker.com/engine/network/drivers/none/)。

## 验证与发布门槛

在仓库根目录执行：

```sh
uv run --active poe lint
uv run --active poe typecheck
uv run --active poe test --disable-warnings
R2_DOCKER_SMOKE=1 uv run --active poe test tests/test_r2_boundaries.py --disable-warnings
```

Docker 冒烟显式启用，使用本地 `kromiose/nekro-agent-sandbox:0.7.22-amd64` 镜像，
不下载镜像、不发起模型请求、不发送平台消息，只使用临时容器和测试目录。
覆盖真实离线 RPC、只读上传、只有回环网络、同任务两轮文件保留。
其余测试覆盖 JSON 限制、撤销/过期、跨会话拒绝、方法更新、HTTP 入口、取消、
重启恢复/清理、文件安全和输出限额。

生产发布前仍需：实际启用插件的 JSON ABI / 目标参数清单、当前沙盒镜像依赖验收、
宿主目录映射与 AppArmor 兼容、磁盘硬限制决策，以及受控回归和回滚窗口。
前后端 RPC 必须与 runner 同版发布，不能让旧 pickle 客户端调用新入口。
不要通过恢复全局 RPC 密钥或不安全反序列化来处理兼容问题。

后续第三、四阶段的 Core 投递扩展、完整幂等回执、收费工具预算与插件逐项迁移
没有在本次完成；R2 整体不因此标记为产品验收通过。
