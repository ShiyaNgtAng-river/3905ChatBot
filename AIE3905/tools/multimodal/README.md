# 群聊多模态适配 v0.1.0

这是独立的本机服务和 AstrBot 插件，不导入或改写原群记插件、数据库或记忆调度。第一阶段先让已有文本模型能按需读取截图文字和录音；云端视觉理解与生图接口已准备，等待账号和密钥。

## 当前能力与边界

| 能力 | 实现 | 账号 | 验证情况 |
|---|---|---|---|
| 图片文字识别 OCR | macOS Vision | 不需要 | 本机真实识别 + HTTP + AstrBot 适配 + DeepSeek 工具调用通过 |
| 语音转写 ASR | faster-whisper base / CPU int8 | 不需要，首次下载公开权重 | 中文合成录音链路通过，有少量错字 |
| 场景、图表等视觉理解 | 兼容 Chat Completions 的视觉服务 | 需要 | 请求契约测试通过，尚未真实调用 |
| AI 生图 | DashScope Qwen Image / SiliconFlow / 通用 images API | 需要 | 请求契约测试通过，尚未真实生成 |

OCR 只能提取文字，不等于看懂图片；ASR 不包含声纹、说话人分离或情绪识别。默认单文件 20 MiB，图片不超过 2500 万像素，本地音频最长 5 分钟。离线 ASR 同时只处理一条，多余请求返回 busy。不会自动重试收费请求。

服务的工作过程：

```text
QQ 当前/引用的图片或语音
  → 新插件取附件（仅完整 UMO 白名单中的会话）
  → 本机 HTTP 服务：OCR / ASR
  → 返回识别文字 + 原消息 ID + 内容哈希
  → DeepSeek 按工具结果回答

明确命令 /识图、/转写 → 直接返回识别结果，只回复一次
云端视觉或生图 → 另配服务和 Key 后启用
```

识别结果标记为派生内容；不会自动写入长期记忆或正式事项。识别服务临时文件随请求删除，不保存媒体和结果。AstrBot 自己的媒体缓存、会话历史、日志仍遵守宿主的保存策略。自然对话时，识别文字会随工具结果提交给现有云端文本模型；“本地 OCR/ASR”不表示后续问答也离线。HTTP 的 source 字段供可信插件传递溯源信息，并非独立身份认证；服务只应由同机可信客户端访问。

## 安装与运行

要求 macOS、Swift Command Line Tools、Python 3.12、uv；合成测试录音另需 ffmpeg。生产 AstrBot 的 Python 环境保持原样。

在项目根目录执行：

```sh
uv venv --python 3.12 .sandbox/groupmedia/venv
uv pip install --python .sandbox/groupmedia/venv/bin/python -r tools/multimodal/requirements.lock.txt
.sandbox/groupmedia/venv/bin/python tools/multimodal/prepare.py --home .sandbox/groupmedia
PYTHONPATH=tools/multimodal .sandbox/groupmedia/venv/bin/python -m groupmedia serve --home .sandbox/groupmedia --port 8792
```

模型只在 prepare 阶段联网下载；识别阶段指定 local_files_only。prepare 会记录权重仓库 revision 和文件 SHA256；可用 `--revision <commit>` 复现。`--model small` 可下载较大模型，但需要手动修改已有 config.json 的 model_path，并重新评测质量和耗时。

服务只监听 `127.0.0.1:8792`，每个接口都需要 Bearer 令牌。首次启动在运行目录生成 `access.json`，文件权限 0600，勿上传 Git。控制台只输出路径，不输出令牌。Ctrl-C 可停止；服务不是开机自启任务。若开发工具沙盒限制 macOS Vision 或端口监听，应从普通终端启动。

本轮已准备的运行目录是项目根目录下 `.sandbox/multimodal-20261002`，可将上述命令的 `.sandbox/groupmedia` 替换为它，复用已下载的依赖、权重和配置。

## 接入 AstrBot

独立插件源码在 `astrbot_plugin_groupmedia/`。通过 AstrBot 本地插件上传功能导入对应 ZIP，或把该目录复制到宿主 `data/plugins/` 后载入。只需安装插件的 httpx 依赖；ASR 和 OCR 依赖留在独立服务环境。

插件配置：

```json
{
  "access_token_file": "/绝对路径/运行目录/access.json",
  "allowed_sessions": ["平台实例ID:GroupMessage:群ID"]
}
```

完整 UMO 从目标测试群的 `/sid` 取得；默认列表为空，所有会话禁用。私聊也只能逐项显式添加完整 UMO。服务与 AstrBot 须在同一台主机、可访问相同文件路径；Docker 跨容器暂不适用。本插件没有修改 QQ 适配器；引用附件、语音格式能否获得，还需要对应平台实群验证。

使用方法：

- `/多模态状态`：检查能力是否已配置。
- 图片 + `/识图`，或引用图片后 `/识图`：提取文字。
- 引用语音后 `/转写`：提取语音文字。
- 图片 + `@机器人 帮我读一下这张通知，时间地点是什么？`：主模型按需调用 OCR 工具，再组织回答。
- `/理解图片`：调用另行配置的视觉模型。
- `/画图 图片描述`：调用另行配置的生图服务。

工具接口为 `groupmedia_ocr(index)`、`groupmedia_describe(index, question)`、`groupmedia_transcribe(index)`、`groupmedia_generate_image(prompt)`。编号从 1 开始，先引用媒体，再当前媒体。工具不接受任意文件路径、网络地址或群号。单条消息最多四次不同任务，相同工具参数复用本轮结果；失败也不会在同轮重复重试。明确命令拦截默认回答链路，避免再生成一条普通回复。

自然语言工具调用依赖 AstrBot 原生 agent 路径及可见的插件工具；若当前人格限制工具列表，需加入上述工具名。群记插件自有的旧 JSON 对话路径没有增加这些工具，建议用原生 `dialogue.frontend=astrbot`。独立插件不自动修改人格、主模型、群列表或既有工具路由。

卸载与恢复：停用/卸载这个新插件，停止本机服务即可恢复接入前的行为；原群记代码与数据库无须恢复。不要删除原群记数据库。

## HTTP 与 CLI 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/capabilities` | 配置与依赖是否就绪；云端配置就绪不代表已经实测 |
| POST | `/api/ocr` | 图片文字识别 |
| POST | `/api/describe` | 视觉理解 |
| POST | `/api/transcribe` | 语音转写 |
| POST | `/api/generate` | 图片生成 |

请求示例（不包含真实数据或密钥）：

```json
{
  "source": {"group": "test:GroupMessage:demo", "message_id": "m-001"},
  "media": {"mime": "image/png", "base64": "文件内容的base64"},
  "prompt": "可选问题"
}
```

generate 不传 media，仅传 prompt 和 source。结果为 `groupmedia-result-v1`，包含 ok、operation、backend、text、source、derived、memory_written、elapsed_ms；OCR 另有行坐标，ASR 另有时间片段，生图另有 asset（mime/base64）。失败返回非 200 及 `error.code/message`，常见 400 输入无效、401 未认证、413 过大、429 忙、503 未配置、504 超时。

```sh
PYTHONPATH=tools/multimodal .sandbox/groupmedia/venv/bin/python -m groupmedia capabilities --home .sandbox/groupmedia
PYTHONPATH=tools/multimodal .sandbox/groupmedia/venv/bin/python -m groupmedia ocr /path/notice.png --home .sandbox/groupmedia
PYTHONPATH=tools/multimodal .sandbox/groupmedia/venv/bin/python -m groupmedia transcribe /path/voice.wav --home .sandbox/groupmedia
```

云服务配置模板见 `cloud.example.json`。Key 通过配置指定的环境变量读取，不放源码、插件配置或打包文件。启动服务前设置 `GROUPMEDIA_VISION_KEY`、`GROUPMEDIA_ASR_KEY`、`GROUPMEDIA_IMAGE_KEY` 中需要的项即可；账号区域、模型开通情况、计费和数据处理地域须按实际账号选择。配置改变后重启独立服务。当前模板中三类服务可以单独启用，不必一起开通。

## 验证与可复现性

服务测试（假云端响应；绑定临时本机端口）：

```sh
PYTHONPATH=tools/multimodal .sandbox/groupmedia/venv/bin/python -m unittest discover -s tools/multimodal/tests -p test_service.py -v
```

AstrBot 契约测试：用宿主 Python，将宿主目录和 `tools/multimodal` 的绝对路径加入 PYTHONPATH，并把 ASTRBOT_ROOT 指向独立空目录，同时将工作目录切到该私有目录，再用绝对路径指定测试文件位置，运行 `test_astrbot.py`。宿主导入时还可能在当前工作目录产生会话临时文件。这样使用真实宿主的组件、装饰器和事件结果类型，但不启动线上 Bot。

`make_fixtures.py --home ...` 生成虚构截图和合成语音。启动服务后，用同样隔离的宿主 Python 运行 `check_live.py --home ...`，验证真实本地识别与插件调用；加 `--deepseek-config <宿主cmd_config.json>` 才会进行四次真实 DeepSeek 调用，只传虚构识别文字。每次测试输出 `smoke-results.json`，保留样本哈希、识别结果、调用量与耗时，不保存凭据。

这些是功能冒烟测试，不是准确率测评，也没有走 QQ 网关。仍待验证：真实群截图、压缩模糊图、口音和噪声、多人说话、QQ 语音转码与引用附件、正式环境下的工具路由。

## 采用的接口依据

- [Apple Vision 文字识别](https://developer.apple.com/documentation/vision/recognizing-text-in-images)
- [faster-whisper 官方仓库](https://github.com/SYSTRAN/faster-whisper)
- [Qwen Image API](https://help.aliyun.com/zh/model-studio/qwen-image-api)
- [Qwen VL OCR](https://help.aliyun.com/zh/model-studio/qwen-vl-ocr)
- [SiliconFlow 语音转写 API](https://docs.siliconflow.cn/docs/api/audio-transcriptions-post)

依赖约束：实测 faster-whisper 1.2.1 尚调用 PyAV 的 metadata_errors 参数，因此限制 PyAV <19，锁文件记录本轮已验证版本。
