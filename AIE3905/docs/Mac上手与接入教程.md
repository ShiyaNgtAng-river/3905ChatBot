# Mac 上手与接入教程

适用于 v0.1.1。按顺序完成，每一步都先检查预期结果。前两步不需要 QQ 账号，也不需要 GPU。

## 1. 先认识三个程序

| 程序 | 负责什么 | 地址／入口 |
| --- | --- | --- |
| 本包的独立实验台 | 本地记事、查看原文、模拟回放 | `./run`；审核页默认 `127.0.0.1:8765` |
| AstrBot 宿主 | 登录消息平台、接收 QQ 事件、装载插件、发送回复 | 独立安装；管理页一般为 `localhost:6185` |
| 云模型服务 | 从文字提取事件、选择回答事实 | 服务商 HTTPS API，通常端口 443 |

审核页的 `/api/ingest` 是本机管理接口，不是 QQ Webhook。QQ 由 AstrBot 适配器接入，不能把 QQ 回调地址直接填成审核页地址。

## 2. 第一次本地运行

解压完整包，在终端进入包含 `run` 的目录。当前交付工作目录为 `/Users/tangshiyang/Documents/ChatGPT/AIE3905`；发给组员后使用他自己的解压路径。

```sh
cd /Users/tangshiyang/Documents/ChatGPT/AIE3905
chmod +x run
./run doctor
./run demo
./run serve
```

`doctor` 应显示 Python 至少 3.11。本机系统自带的 Python 3.9 不够；脚本会尝试已有 Anaconda Python。可通过 `GROUPBOT_PYTHON` 指定解释器。创建的 `.venv` 仅供本包使用。

打开 http://127.0.0.1:8765，输入终端打印的工作台令牌并连接。这个令牌不是云模型 API Key。没有先执行 `demo` 时，点击“导入虚构演示”。在“问一问”输入：

```text
产品演示现在怎么定的？
产品演示为什么改期？
还有什么待确认？
```

检查回答是否同时区分：负责人确认的改期、某位成员的缺席、尚未获确认的取消建议。点“原文”核对依据。在“记事、确认与其他操作”中输入 `/秘书帮助` 查看命令。

`demo` 使用持久化学习库，重复执行不会重复导入相同演示 ID；`bench` 每次使用临时空库。终端按 `Ctrl+C` 停止网页，数据保留在 `data/demo.sqlite3`。若端口被占用，先停止旧服务；不要运行两个进程写同一数据库。

## 3. 看懂模型接口

配置文件里最重要的字段如下：

| 字段 | 意义 | 常见错误 |
| --- | --- | --- |
| `mode` | `demo`、`openai` 或 `astrbot` | 把 `openai` 误认为只能用 OpenAI 服务 |
| `base_url` | 服务前缀，程序会追加 `/chat/completions` | 填了完整接口，导致重复追加 |
| `model` | 服务商认可的模型 ID | 填网页产品昵称或不可用的旧模型别名 |
| `api_key_env` | 从哪个环境变量读取 Key | 把 Key 本身填在这里 |
| `allow_remote` | 显式允许调用外部模型 | 外部地址未启用时会被程序拒绝 |
| `json_mode` | 请求 JSON 对象输出 | JSON 合法不代表事实正确 |
| `extra_body` | 本版支持的思考开关 | 本包只允许 `thinking`、`enable_thinking`，不支持任意 SDK 参数 |
| `models.understanding` | 事件抽取模型 | 可以与 answering 相同 |
| `models.answering` | 回答事实选择／闲聊模型 | 不是另一个必须购买的服务 |

原始请求大致为：

```json
{
  "model": "服务商模型ID",
  "messages": [
    {"role": "system", "content": "从群聊提取事件，只输出 JSON ..."},
    {"role": "user", "content": "当前消息、历史与候选事项的 JSON"}
  ],
  "response_format": {"type": "json_object"},
  "temperature": 0.2,
  "max_tokens": 1800
}
```

本版把 `extra_body` 中的开关合并到请求顶层；不会真的发送名为 `extra_body` 的嵌套字段。

## 4. 连接 DeepSeek 或 Qwen

先使用普通模型 API 服务，在控制台创建 Key 并确认余额／可调用模型。网页聊天订阅与 API 服务不是同一个接入方式。不要把 Key 发到聊天或写进要交付的 JSON。

本版提供两份预设，按 2026-09-28 查阅的官方文档整理；实际可用性以账号控制台和连通测试为准：

| 配置 | Base URL | 模型 | 额外参数 |
| --- | --- | --- | --- |
| `config/deepseek.json` | `https://api.deepseek.com` | `deepseek-flash` | `thinking: {type: disabled}` |
| `config/qwen.json` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | `enable_thinking: false` |

DeepSeek 的 JSON 模式与思考参数见 [JSON 输出](https://api-docs.deepseek.com/guides/json_mode/) 和 [Chat Completion 接口](https://api-docs.deepseek.com/api/create-chat-completion/)。不要直接沿用旧教程的模型名称。

Qwen 预设使用北京地域的兼容地址。阿里云文档同时推荐业务空间专属域名；新加坡等其他地域应替换为对应地址，Key 与地域必须匹配。现有 `dashscope` 域名仍可使用。见 [兼容接口](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions) 和 [结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)。

先选择一家运行：

```sh
./run smoke --config config/deepseek.json
# 或
./run smoke --config config/qwen.json
```

终端会隐藏询问 `GROUPBOT_MODEL_API_KEY`。输入后仅存在于这个进程内；成功应显示“API 与 JSON 输出连通”。本步只发一个虚构请求，验证地址、Key、模型和 JSON 格式，不能证明群聊理解质量。

然后运行已准备好的 12 条自然语言消息小样本：

```sh
./run bench --config config/deepseek.json --dataset datasets/natural-small --output results/deepseek-small.json
./run bench --config config/qwen.json --dataset datasets/natural-small --output results/qwen-small.json
```

第二条需要换成 Qwen 的 Key。若已经导出了同名环境变量，程序会优先使用它；切换厂商时先 `unset GROUPBOT_MODEL_API_KEY`，再让程序询问，避免误用旧 Key。每次评测创建独立空库，因此两个模型可比较同一组输入。

常见问题：`HTTPError` 可对应鉴权、额度、模型／区域或请求参数错误；原实现不会把服务响应正文写入日志。优先核对上述四项与服务商控制台。`URLError` 检查网络、代理与证书；空内容或非 JSON 检查模型支持、思考开关和输出 token 限额。不要把完整授权头粘贴到排障记录。

## 5. 在 Mac 安装 AstrBot

实验台的 Python 环境与 AstrBot 环境分开。两种方式均可，本地入门优先桌面版；需要追踪代码、终端环境变量时选源码方式。

**桌面版：**从 [AstrBot 官方桌面部署说明](https://docs.astrbot.app/deploy/astrbot/desktop.html) 进入官方仓库，下载适合 Mac 架构的版本，启动后打开管理界面。文档支持 macOS；本次没有安装或验证实际安装包。

**源码方式：**按 [源码部署说明](https://docs.astrbot.app/deploy/astrbot/cli.html) 准备 Python 3.12+ 和 uv，然后执行：

```sh
git clone https://github.com/AstrBotDevs/AstrBot.git
cd AstrBot
uv sync
uv run main.py
```

后续启动可按官方说明使用 `uv run --no-sync main.py`，避免同步时移除插件另装依赖。管理页一般为 http://localhost:6185；使用实际启动日志显示的登录信息，不假设固定初始密码。记录安装的 AstrBot 版本；插件声明要求至少 4.28.1。

## 6. 装插件和配置模型

在 AstrBot 的插件页，用文件安装功能选择 **`astrbot_plugin_groupsecretary_v0.1.1.zip`**。不要上传完整学习包。安装入口见 [官方 WebUI 说明](https://docs.astrbot.app/use/webui.html)。

首次插件未配置群时不会开始采集。插件设置只有 `config_path`：填主配置 JSON 的**绝对路径**。不要填 ZIP 路径，也不要直接指向未替换占位符的模板。

桌面版推荐宿主管理模型：在 AstrBot 的模型服务页配置 DeepSeek／Qwen 的地址、Key、模型，并设置该服务支持的非思考参数。记录 **Provider ID**。复制 `config/qq.astrbot.template.json` 为 `config/qq.json`，将 understanding 与 answering 的 `provider_id` 都填为实际 Provider ID。这里填宿主服务实例 ID，而不是 `qwen-plus` 这样的模型 ID。参照 [接入模型服务](https://docs.astrbot.app/providers/start.html)。

源码启动也可用 `mode: openai`，复制 `qq.template.json` 并把两处模型设置换成已验证的 DeepSeek／Qwen 配置。通过终端隐藏输入并导出 Key 后，在**同一终端**启动宿主：

```sh
read -s 'GROUPBOT_MODEL_API_KEY?请输入模型 API Key: '
printf '\n'
export GROUPBOT_MODEL_API_KEY
uv run --no-sync main.py
```

以上 `read` 语法用于 macOS 默认 zsh。双击启动的桌面应用通常不会继承某个终端临时导出的变量，因此桌面版优先使用宿主管理 Provider。

`web.enabled` 初始保持 false。等 QQ 接入成功后，若希望同时看本插件审核页，可给宿主进程设置 `GROUPBOT_ADMIN_TOKEN` 并启用 web；也可只使用 QQ 命令。不要另开实验台进程连接同一个 QQ 数据库。

## 7. 连接真实 QQ 测试群

本次建议先试 **QQ 官方 WebSocket**。当前 AstrBot 文档提供扫码一键创建，以及群内全部消息的设置；是否能在你的账号和群上启用仍需实测。[官方接入说明](https://docs.astrbot.app/platform/qqofficial/websockets.html)

1. AstrBot 管理页：机器人 → 创建机器人 → QQ 官方机器人（WebSocket）→ 扫码一键创建。
2. 在手机 QQ 完成扫码，保存平台实例。把实例 ID 记下来，例如 `qq-pilot`。
3. 从手机 QQ 的机器人资料页添加到测试群。当前文档说明仅能添加到自己为群主的群。
4. 在该群的机器人设置里选择可获取群内全部消息。是否启用主动发言由测试需要决定；本插件 `proactive` 默认 false。
5. 在测试群由你手动发送 `/sid`。记录 Bot ID、UID 和实际群 ID；隔离会话开启时不要把含用户的 Session ID 当成群 ID。[指令说明](https://docs.astrbot.app/use/command.html)
6. 修改 `qq.json`：`platform_id` 填平台实例 ID；`native_group_id` 填实际群 ID；`admins` 和 `confirmers` 填稳定 UID。`key` 是插件内部自定义名称，例如 `qq-pilot`，不必等于平台类型名。
7. 明确测试群的数据处理范围与云服务后，将该群的 `enabled` 和 `data_use_confirmed` 都设为 true；填写实际 `processing_location`。本轮先保留 `proactive: false` 和空 `report_time`。
8. 将插件的 `config_path` 指向这个文件，保存并重载插件。若宿主启用白名单，把测试群加入白名单。

官方机器人群 ID／用户 ID 可能是平台返回的标识，不能凭平时看到的 QQ 号码猜。普通消息收不到时先检查 QQ 的消息范围和宿主白名单，不要先改模型提示词。

如果官方路线不可用，再考虑 **NapCat + OneBot v11**。它涉及 QQ 客户端侧桥接；在 Mac 上通常还需要单独的 Linux／容器环境，不能假设本包会安装它。AstrBot 创建 OneBot v11 适配器作为反向 WebSocket 服务端，客户端连接 `ws://宿主地址:6199/ws`，两端 token 保持一致；容器中的 localhost 指容器自身。以 [AstrBot OneBot 文档](https://docs.astrbot.app/platform/aiocqhttp.html) 和 [NapCat 集成文档](https://napneko.pages.dev/use/integration) 为准。当前未验证此路线。

## 8. QQ 的最小验收

由测试群成员手动执行，逐项记录结果：

| 操作 | 应看到什么 |
| --- | --- |
| 不 @ 机器人，发一条普通工作讨论 | 在插件原文或审核页能找到，且不会逐句打断回复 |
| 普通成员提出带唯一标题的安排 | 处于待确认状态 |
| 配置中的确认人引用提议回复“可以” | 更新正确事项，来源包含提议与确认 |
| 成员说“这次我不来” | 个人缺席，事项仍有效 |
| 非确认人建议取消 | 不直接覆盖正式决定 |
| `/问 事项标题现在怎么定的` | 回答有来源，可检查当前状态 |
| 撤回关键变更消息 | 收到真实撤回事件后证据失效；若仍有效，说明桥接尚未接通 |
| 同一事件重复投递／宿主重启 | 不重复生成事件，未完成工作可恢复 |

插件已经有 `ingest_recall` 入口，但适配器必须真的调用或送达对应通知。用文字说“撤回”或在模拟器产生 recall，不能证明真实 QQ 撤回事件已接通。只有所有关键项完成后，才记为 QQ 联调通过。
