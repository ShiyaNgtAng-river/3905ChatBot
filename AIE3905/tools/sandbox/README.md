# 沙盒端到端测试

在一台机器上跑起**真实的 AstrBot（固定 `v4.28.1`）+ 真实的 OneBot v11 适配器 + 本插件**，用脚本扮演 NapCat／QQ 群成员发消息、引用、@、撤回，再检查插件的数据库与回复。只有 QQ 服务器本身是模拟的；模型可以是本地假模型，也可以是真实的 DeepSeek／Qwen。

## 运行

在 `AIE3905/` 目录：

```sh
python3 tools/sandbox/run.py                         # 本地假模型：不需要 Key，不产生费用
python3 tools/sandbox/run.py --model deepseek        # 真实 DeepSeek，经 AstrBot 管理的 Provider 调用
python3 tools/sandbox/run.py --model qwen            # 真实 Qwen（默认北京地域兼容地址）
```

- 首次运行会把 AstrBot 克隆到 `.sandbox/astrbot` 并用 `uv sync` 安装（约 500 MB，需要 git、uv；Python 3.12 由 uv 选择或下载）。之后复用。
- 真实模型从环境变量 `GROUPBOT_MODEL_API_KEY` 读取 Key。脚本只把变量名 `$GROUPBOT_MODEL_API_KEY` 写进沙盒里的 AstrBot 配置，Key 本身不会落盘或进入报告。
- 模型 ID、地址默认取 `config/deepseek.json`、`config/qwen.json`；与控制台不一致时用 `--model-id`（或环境变量 `GROUPBOT_MODEL_ID`）和 `--base-url` 覆盖。
- 真实模型运行前先发一个极小的连通请求，Key、模型 ID、余额或网络有问题会直接给出原因并停止。
- 沙盒使用 16199／16185／16800 端口，不影响本机已在 6185／6199 运行的 AstrBot。每次运行清空 `.sandbox/work-<model>/`。

## 检查内容

报告写入 `results/sandbox-<model>.json`，检查分两类：

| 类别 | 含义 | 例子 |
| --- | --- | --- |
| 系统 | 与模型无关，必须全部通过；任一失败则退出码为 1 | 插件加载、普通消息不回复、`/问` 和 @ 只回一条、重复投递、撤回、命令、`/别记我`、重启 |
| 理解 | 取决于模型对自然语言的理解；真实模型下是效果观察，不是程序错误 | 提议／确认／个人缺席／非确认人取消／改期是否被正确记录，回答是否给出新时间 |

`observations` 还记录回复延迟、各类模型调用次数与耗时、`/sid` 的回复（插件会额外回“未识别的命令”），假模型模式下还记录模型持续故障多久后消息被标为失败、恢复后是否重试。

一次真实模型运行约 20–30 次模型调用，数据全部虚构。

## 不覆盖的部分

- QQ 服务器、NapCat 客户端本身、QQ 官方机器人（`qq_official`）适配器。插件只识别 OneBot 的撤回通知，官方机器人渠道的撤回需要另行验证。
- 大群容量、长期运行和并发。
