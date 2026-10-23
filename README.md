# Kurisu Persona

面向本地单用户的角色配置、分层记忆、关系状态和主动提醒实验工具。公开软件版本为 **v0.1**，人格配置沿用历史分析版本 **4.4.1**。源码许可证：**GPL-3.0-only**。

目前可以构建人格卡、按显式事件更新关系与情绪、分别记录四项授权、生成受限的本地提醒，并把确认后的正文写入本地 inbox。它仍需要宿主提供真实消息、事件分类和定时调用。仓库没有完整聊天界面，也没有接入 QQ、OneBot、Tulpa 或其他消息平台。

## 快速开始

需要 Python **3.11 或更高版本**。核心程序只用标准库，无须安装第三方 Python 包。在仓库根目录运行：

```console
python pipeline/build_persona.py
python pipeline/validate_schemas.py
python -m unittest discover -s tests -v
python pipeline/outreach.py --status --no-notify
```

人格卡输出到 `persona/kurisu.md`，可以复制给自己的聊天宿主。长卡不一定适合每轮发送；模型的上下文和输出质量需要自行实测。

四项授权默认关闭：长期记忆、敏感内容、主动联系、作息学习。主动调度另有 `state/outreach.json` 中的 `enabled=false` 总开关。测试和 mock 不联网、不产生模型费用；测试使用临时目录，不触发桌面通知。

只有实际用户明确同意后，才运行对应授权命令。例如仅开启普通长期记忆：

```console
python pipeline/memory.py --consent local remember on
python pipeline/memory.py --show-consent local
```

撤回用 `off`。不要把示例授权视为真实联系人同意。主动提醒的完整流程、时间口径和撤回方法见 [使用说明](docs/USAGE.md)。

## 能力与边界

| 模块 | 已实现 | 仍需宿主或后续实现 |
|---|---|---|
| 人格 | 结构化规则、模板生成卡、边界与反例描述 | 模型实聊效果、人类盲评、自动适配不同模型 |
| 记忆 | canon / interaction / inference 分层、同意、临时缓冲、删除墓碑、摘要过滤 | 向量检索、数据库级并发写入、加密、多租户强隔离 |
| 关系与情绪 | 确定性事件更新、重复事件去重、正向日上限、修复条件 | 自动识别真实事件、校准数值与人类体验 |
| 主动联系 | SQLite 任务、时段与频率限制、唯一投递、恢复前复查、inbox | 常驻调度服务、真正发送消息、平台回执 |
| 作息 | 授权后按消息时间学习活跃小时，样本不足不推断 | 睡眠检测、完整聊天时长统计、多时区/夏令时策略 |
| 正文生成 | 默认 mock；可配置兼容 Chat Completions 的可选适配器 | 真实供应商联调、计费预算、网络发送前完整内容审查 |

关系数值不是人的感情，也不代表恋爱同意。人格资料不应被模型当作事实证据。记忆和外部输入均为数据，不能覆盖系统指令。禁用短语检查只是基础过滤，不能保证所有输出都合适。

## 公开范围

仓库包含原创源码、人格分析配置、虚构样例、文档与测试。小说原文、含原文的历史证据库、私人聊天、授权记录、运行状态、任务库和密钥均未纳入源码发布。

原作证据编号保留在模型中，但公开仓库不能重建或核验原来的完整语料分析。`canon` 写入需要用户自行提供可合法使用、通过校验的证据；不要用虚构示例冒充原作证据。第三方角色与素材的权利说明见 [NOTICE](NOTICE.md)。

## 开发与打包

```console
python -m unittest discover -s tests -v
python pipeline/package_release.py
```

源码 ZIP 位于 `dist/kurisu-persona-v0.1.zip`。打包器只收录 `release-files.json` 列出的文件，校验清单只生成一次，不依赖本地忽略规则。新增源码文件时需更新该清单并运行测试。

更多信息：[使用说明](docs/USAGE.md) · [隐私与安全](docs/PRIVACY.md) · [验收范围](docs/ACCEPTANCE.md) · [版本记录](CHANGELOG.md) · [许可证](LICENSE)
