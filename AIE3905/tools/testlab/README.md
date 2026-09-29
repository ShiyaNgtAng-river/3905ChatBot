# GroupBot 内部测试台

独立于业务代码的本机网页测试程序。启动真实 AstrBot 和插件副本，用虚拟 OneBot 投递消息；不登录 QQ、不发送真实群消息。可通过网页手动扮演群成员，也可导入测试集并行回放，模型真实调用现有 DeepSeek 服务。

## 启动

在包含 `plugin/`、`config/` 和 `tools/` 的项目目录运行；从 GitHub 克隆仓库后，先 `cd AIE3905`。

需要一套已经安装依赖和面板静态文件的 AstrBot。`--model host` 还要求宿主已配置模型服务，以及本项目已有本机 `config/qq.json`（不纳入 Git）；测试台只读取其中的 Provider 路由。首次准备该文件可参考 `config/qq.astrbot.template.json`，把 Provider ID 对应到宿主已配置的服务。无需配置或登录 QQ，测试群配置由测试台单独生成。`--model mock` 不需要模型 Key 或 `config/qq.json`。

```sh
cd AIE3905  # 已经处于项目目录时跳过
```

启动测试台：

```sh
python3 tools/testlab/run.py \
  --astrbot /Users/tangshiyang/CUHKSZ/AstrBot/AstrBot-master \
  --model host --parallel 2 --port 8787
```

打开终端打印的完整链接。链接里的本地访问令牌由页面存入会话存储，并从地址栏移除。也可在 `.sandbox/testlab/access.json` 找到启动链接；不要把这个文件提交到 Git。使用假模型验证管道时将 `--model host` 换成 `--model mock`。无需安装新依赖，入口自动使用指定 AstrBot 的虚拟环境。

所有新增程序都在 `tools/testlab/`。不会修改原插件、原沙盒脚本、AstrBot 源码或线上配置。每个批次：

- 使用独立的 `ASTRBOT_ROOT`、插件副本、SQLite 数据库、端口、日志和模拟消息编号空间；源文件 SHA-256 清单随报告保存。
- 只从宿主读取所需模型服务及 Key；Key 经子进程环境变量传递，生成配置保存变量名。不复制 QQ 凭据和真实群数据库。
- 使用仓库里的爱音人设和记忆子智能体配置；日常／深入模型选择映射到当前业务配置。新建批次才读取最新源码与配置，已经启动的批次保持原副本。
- 安装一个测试专用桥接插件。它只在隔离进程中包装管道完成事件和 Provider 调用，用于准确判定一轮结束、收集用量、执行调用次数上限。它不进入生产插件目录，不改宿主源码文件。当前验收宿主为 AstrBot 4.28.1，升级宿主后需重跑集成检查。

## 网页操作

1. 点“开一个交互群”，等待“可交互”。选择老张（确认人）或普通成员，勾选 @ 后提问；不勾选时模拟普通群消息。
2. 回复旁显示模拟消息编号，可填入“引用编号”。观察草案、正式事项、模型调用、工具参数及返回值。
3. 选一个日期，点“通读这一天”或“日终整合”，对比记忆快照变化。测试里关闭自动记忆调度，避免后台额外调用和不可重复的时间因素；按钮使用真实插件方法和真实 Provider。
4. 批量模式可粘贴 JSON 数组、JSONL，上传文件，或选择仓库测试集。副本数1–8，默认最多2批同时运行；更多批次排队。同一批内逐条等待完整处理结束。
5. “停止”关闭该批次进程并保留报告。交互群会占用一个并发名额，用完请停止。服务器 Ctrl+C 会停止所有隔离子进程。
6. “导出 JSON”保存完整收发时间线、模型用量、工具记录、断言结果、源码清单和数据库快照。每张快照表最多500条，完整测试数据库保留在该批次目录。

程序只监听 `127.0.0.1`。API 需要 Bearer 令牌，拒绝带有其他 Origin 的浏览器请求。默认只运行你显式创建的批次，打开网页本身不调用模型。

## 测试数据

```json
[
  {"sender":"lin","native_id":"m1","text":"建议虚构项目周五下午讨论，还没确定。","expect":{"reply_count":0}},
  {"sender":"owner","text":"帮我们设计两套方案","at":true},
  {"sender":"owner","text":"第二个方案提前半小时","at":true},
  {"sender":"owner","text":"就按刚修改的第二个方案定了","at":true},
  {"sender":"lin","text":"最终怎么安排？","at":true}
]
```

字段：

| 字段 | 含义 |
|---|---|
| `sender` / `name` | 稳定成员标识／显示名称；`owner` 是测试确认人，其余标识是普通成员 |
| `text` | 消息文字；当前回放不支持实际附件，附件需改为明确的文字占位 |
| `at` 或 `mention` | 布尔值，是否 @ 机器人；默认 false |
| `timestamp` | 可选，带时区的 ISO 日期时间；保留原日期，不加速系统时钟 |
| `native_id` | 可选稳定消息编号；重复编号可测试幂等 |
| `reply_to` | 引用先前的 `native_id`，或网页显示的模拟消息编号 |
| `kind:"recall"`、`target_id` | 撤回已经发送的消息 |
| `expect` | 可选 `reply_count`、`contains`、`not_contains`；后两项是字符串数组 |

兼容现有 `messages.jsonl`：字符串 `at` 自动转换为 `timestamp`，`kind:create` 视为普通消息。原记录的 `group` 会映射到该批次唯一的虚拟群。`oracle.json` 和 `cases.json` 不进入模型；这个测试台只执行消息里显式写出的 `expect`，不会冒充已有语义评测器的结果。长记忆的专门指标仍使用原有 `tools/eval_memory.py`。

勾选“每个日期结束时执行通读和整合”，按输入顺序在日期切换及最后一天结束时执行这两步。数据应按时间排列，使用历史或当天日期；包含未来日期的记忆读取受原插件当前时钟约束。用于隔离回放的保留期为3650天，线上保留期不变。

## 并行与预算

- `--parallel 1..4` 控制同时存在的隔离机器人实例。每个实例单独生成回答，结果互不共享；API账号额度仍然共享。
- 每批默认最多80次 Provider 调用，网页可设1–1000。用完额度后再尝试调用会被拒绝并报告失败；恰好用完额度但已完成的批次仍然成功。这个计数包含主对话、后台读取、日终整合和子智能体；不等于供应商账单请求数，SDK内部重试可能产生额外请求。
- 每批上限20000条消息，单轮等待上限240秒，批量回放上限30分钟。大型数据集先切成合理批次；跨批记忆独立，测试跨周记忆时应放在同一批。
- 统计每轮完整处理耗时 p50/p95、模型调用耗时、输入／输出 token、缓存命中量及比率。没有服务商单价信息，不估算金额。缓存统计依赖宿主 Provider 实际返回的字段。
- 默认关闭联网搜索、流式输出和按句分段，工具范围限定为群记忆、事项和记忆子智能体，便于对照记忆与完整回答。测试台验证内部流程，不代表 QQ 官方适配器、真实群投递和搜索服务已经验收。

## HTTP 接口与命令行

所有 `/api/` 请求带 `Authorization: Bearer <access.json里的token>`。

| 请求 | 用途 |
|---|---|
| `GET /api/info` | 模型模式、并行度、数据集列表、环境差异 |
| `GET /api/runs` | 批次列表和进度 |
| `POST /api/runs` | 创建实验：`title`、可选 `rows` 或 `dataset`、`copies`、`model_limit`、`integrate`；不传 rows/dataset 为交互群 |
| `GET /api/runs/{id}` | 完整报告，可直接保存 JSON |
| `POST /api/runs/{id}/step` | 交互群发送一条上述格式的消息 |
| `POST /api/runs/{id}/memory` | `{"action":"read或consolidate","day":"2026-09-29"}` |
| `POST /api/runs/{id}/stop` | 停止运行，保留数据 |
| `GET /api/runs/{id}/log` | 最近120行隔离实例日志 |

也可以在服务器启动后通过独立客户端提交批次：

```sh
python3 tools/testlab/client.py tools/testlab/example.json --copies 2 --model-limit 12 --integrate
```

客户端只依赖 Python 标准库。报告位于 `.sandbox/testlab/<批次ID>/report.json`；数据库位于同目录下 `root/data/plugin_data/astrbot_plugin_groupsecretary/secretary.sqlite3`。停止服务器后再启动仍能查看旧报告；正在运行的会话不会自动续跑。

## 验证

```sh
/Users/tangshiyang/CUHKSZ/AstrBot/AstrBot-master/.venv/bin/python -B tools/testlab/test_runtime.py
/Users/tangshiyang/CUHKSZ/AstrBot/AstrBot-master/.venv/bin/python -B tools/testlab/check.py \
  --astrbot /Users/tangshiyang/CUHKSZ/AstrBot/AstrBot-master
```

第一组验证输入、认证、跨站拒绝、路径隔离与排队取消。第二组启动真实 AstrBot 和本地假模型，验证并行、隔离、通读／整合、实际工具调用、正式确认、重复消息、撤回、停止和调用预算。报告写入 `.sandbox/testlab-check-<时间>/verification.json`。
