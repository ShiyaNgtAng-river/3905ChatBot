# 群聊多模态适配 v0.1.0

此 ZIP 仅包含 AstrBot 接入插件，需要同机运行独立 groupmedia 服务。完整服务源码、安装与接口说明位于项目 `tools/multimodal/README.md`。

在插件配置中设置：

- `access_token_file`：服务首次启动生成的 access.json 的绝对路径。只读取本机 127.0.0.1 服务地址和访问令牌。
- `allowed_sessions`：允许使用的完整 UMO 列表，从目标群 `/sid` 获取；默认空列表，所有会话禁用。

命令：`/多模态状态`、`/识图`、`/理解图片`、`/转写`、`/画图 描述`。图片或语音放在当前消息或显式引用的消息中；平台需能提供该附件。模型也可调用 `groupmedia_ocr`、`groupmedia_describe`、`groupmedia_transcribe`、`groupmedia_generate_image`。

本地 OCR 和 ASR 不需要账号；视觉理解与图片生成需另配服务。插件不修改原群记插件，不自动写入正式事项或长期记忆。识别结果在自然对话中会交给当前文本模型。QQ 实群投递与引用附件仍需验收；本轮验证未代发群消息。

恢复：停用此插件并停止独立服务，无须恢复原数据库。
