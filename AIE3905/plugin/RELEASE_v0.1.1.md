# v0.1.1 学习与集成验证版

基于 AIE3905 GroupBot v0.1.0，仅扩展云模型传输参数并更新版本。

`models.understanding` 与 `models.answering` 的设置可以包含：

- Qwen：`"extra_body": {"enable_thinking": false}`。
- DeepSeek：`"extra_body": {"thinking": {"type": "disabled"}}`。

参数仅用于 `mode: openai`，会合并到原始请求体顶层。宿主 `mode: astrbot` 的参数在 AstrBot Provider 中配置。两者都不需要 OpenAI SDK；独立核心仅依赖 Python 标准库。

完整学习交付包另含 Mac 启动工具、合成数据、教程和评测结果。本插件包应通过 AstrBot 插件文件安装功能安装，随后配置 `config_path`。

原 README、架构与测试文档描述原始 v0.1.0 基线。本版仍有更正权限绕过、更正取消原因改变状态、自然语言更正缺少目标 ID、时间解析和同名事项等已知问题。没有完成真实 LLM／QQ 联调，不作为生产可用声明。
