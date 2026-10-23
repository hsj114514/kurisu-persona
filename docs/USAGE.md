# 使用说明

## 本地运行方式

`pipeline/` 的脚本从任何目录启动时，默认仍读写本仓库中的数据路径。建议先建立虚拟环境，并在仓库根目录运行。程序不自带常驻循环、操作系统定时任务或自动回复服务；`--tick` 只处理一次。Windows 桌面通知是可选的辅助功能，其实际显示受系统权限和版本影响，使用 `--no-notify` 可以禁用。

人格卡由 `persona/kurisu_persona_model.json` 与模板生成。`python pipeline/build_persona.py --check` 只检查可构建；重新生成用不带 `--check` 的命令。用户状态可经 `--state state/current.json` 注入，但包含私人状态的生成卡不可提交。

关系状态由宿主提供事件，不会自动理解聊天。宿主需要为每个真实事件提供稳定 `event_id`、带时区的 `occurred_at` 和正确主体；重复事件只计一次。不要让模型凭空编造互动或把助手自己的推测计成用户行为。状态 CLI 见 `python pipeline/state.py --help`。

## 记忆与同意

授权命令 `python pipeline/memory.py --consent local <scope> on|off` 的 scope 为 `remember`、`sensitive`、`proactive`、`schedule`。四项互相独立，开启一个不会自动开启另一个。

长期记忆需要 remember，敏感记录还需要 sensitive。未授权的输入要求 `session_id`，写入带过期时间的会话缓冲，默认 180 分钟。**缓冲仍会写磁盘**；需要完全不落盘的宿主应避免调用该写入函数并自己保持内存。过期过滤不等于物理删除，宿主须调用 `purge_expired()` / `clear_session()` 或主动清理文件。

`python pipeline/memory.py --forget local` 清理该主体的记忆、授权和会话记录，墓碑用于防止被删除的来源事件重导入。另运行 `python pipeline/outreach.py --forget-local --no-notify` 清理本地调度任务、inbox、承诺、事件和通知。自建状态文件需传给 memory 的 `--state-file`；备份、导出和外部平台副本要另行删除。仅撤回授权会停止后续使用，不会自动销毁所有历史副本。

## 主动提醒

先取得真实同意，再开启 `proactive`，并把 `state/outreach.json` 的 enabled 改为 true。默认安静时段 22:00–次日 09:00，每自然日 1 条、滚动 24 小时 1 条、成功后冷却 6 小时，上次未获回复时暂停。

以下时间仅为格式示例，应替换为本地未来时间：

```console
python pipeline/outreach.py --seed --reason follow_up --due 2030-01-01T10:00:00+08:00 --no-notify
python pipeline/outreach.py --tick --no-notify
python pipeline/outreach.py --pending --no-notify
python pipeline/composer.py --compose TASK_ID --dry-run
python pipeline/outreach.py --accept TASK_ID --text "你安排的事到时间了，提醒一下。" --no-notify
```

`TASK_ID` 使用 seed 输出的真实值。未到期时 tick 不会生成 pending。成功确认只表示追加到 `runtime/outreach/inbox.jsonl`，不表示任何人已收到消息。收到真实用户回复后由宿主调用 `--user-said`；不能把助手消息、输入中状态或虚构回复当作用户回复。

未完成投递用 `--recover --no-notify`。恢复前重新检查当前权限与所有发送限制，未通过的意图取消；如仍需提醒，应在允许时重新建立任务。已写入 inbox 但尚未提交数据库的记录只对账，不重复追加。不要手工改 inbox、复制旧数据库覆盖新数据库或让外部进程同时编辑这些文件。JSONL 末行损坏会报错，需先备份再人工修复；本版本不承诺跨文件数据库级原子性。

`--force` 仅允许人工对 pending 任务跳过 tick 阶段，仍受授权、开关、到期、安静时段与频率限制；不会重开 delivered / delivering 等任务。撤回 proactive 后未完成任务即使走 recover 也不能发送。

## 时间和作息口径

- 时间输入必须带 UTC 偏移（如 `+08:00`），调度到期时间会转换为运行系统本地时区。单机部署中保持系统时区不变；迁移时区、夏令时边界和旧数据库需单独迁移验证。
- 自然日以调用时的本地日期计算；滚动 24 小时和冷却按两个带时区时间点之差计算。TTL 从 due_at 起算，默认 24 小时。到期任务不会提前发送，expires_at 时刻及之后拒绝新投递。
- `--now` 是测试时钟，真实运行不要由模型或不可信输入控制。宿主必须使用可信时钟并保持同步。
- `schedule.py --write` 需要 schedule 授权，至少 30 条用户消息且跨 5 个日期才统计小时窗口。小时计数不是睡眠证据；生成的置信度只是样本规模启发值，不是概率。
- 作息学习统一转换到运行系统本地时间，排除过期缓冲、无时区与未来时间。固定安静时段只会被学习窗口收紧，不会放宽；学习结果超过 30 天不使用。
- **v0.1 没有计算聊天总时长或有效陪伴时长。** 消息时间差不能直接等同聊天时长，也不会因为离线时间增加信任。

## 可选联网与费用

默认 provider=mock。若要调用兼容 Chat Completions 的供应商，须由用户自己设置 endpoint、model、指定环境变量中的密钥，并将 composer 的 allow_network 设为 true。不要把密钥写进配置、代码、提示词或 Git。

联网请求会把人格摘要、当前状态、经授权提取的 local 记忆以及任务事项发给所选供应商；开启本地记忆不等同于同意发送给云服务。使用者应先确认发送范围及供应商数据政策。仅允许 HTTPS；该适配器和 DeepSeek 等具体供应商的实时 API、模型名、价格均未在 v0.1 做真实联调。

每次 compose 最多一次请求，无自动重试；默认 max_tokens=160 是输出上限，输入仍可能较长。本版本没有货币预算、日/月账单限额或准确 token 计数。估算费用应使用供应商当前输入/输出单价 × 实际 token 用量，并在供应商侧设置额度。`--dry-run` 表示只输出生成结果、不接受投递；选择联网 provider 时它仍会调用付费 API。

文本抽取工具 `extract.py` 默认只生成本地任务；显式传入 `--endpoint` 会逐场景联网，可能发生多次计费。批量调用前先限制场景数并确认文本可向该服务披露。预处理和证据检查工具供用户自行准备合法本地语料；数据默认放在被忽略的 corpus/、data/ 下。不要将原作材料或带引文数据随源码公开。
