# AIE3905 GroupBot v0.1.1

2026-09-28 · Mac 学习与集成验证版

本版以 `AIE3905_GroupBot_v0.1.0.zip` 为主线，加入 DeepSeek／Qwen 参数兼容、Mac 启动入口、可复现噪音数据生成与评测。真实 LLM、AstrBot 宿主和 QQ 群尚未联调；已知的事项更正与时间解析缺陷仍然存在。

先读 [总结交付文档](docs/总结交付文档_v0.1.1.md)，按 [Mac 上手与接入教程](docs/Mac上手与接入教程.md) 操作。接口和评测细节见 [接口与模拟测试](docs/接口与模拟测试.md)。

## 五分钟本地练习

在终端进入解压后的目录。下面的命令都在本目录执行：

```sh
chmod +x run
./run doctor
./run demo
./run serve
```

`run` 首次使用本机已有 Python 3.11+ 创建隔离环境，无需安装第三方依赖。此 Mac 可自动找到 Anaconda Python 3.13；换电脑时也可执行 `GROUPBOT_PYTHON=/你的/python路径 ./run doctor`。宿主 AstrBot 的 Python 版本要求不同，见教程。

打开 http://127.0.0.1:8765，把终端打印的**工作台访问令牌**填入页面，点击连接。若没有先执行 demo，点“导入虚构演示”。问“产品演示现在怎么定的？”并点击原文检查依据。`Ctrl+C` 停止页面服务。端口被占用时，停止此前的练习服务，或修改配置中的 `web.port`。

## 噪音回放

```sh
./run generate --scenarios 2 --noise 0.8 --seed 42
./run bench
```

默认生成 60 条消息，其中 48 条为噪音；12 个检查点。结果保存到 `results/demo-noise80.json`。这些是确定性命令回归，不是自然语言模型准确率。

## 云模型连通与自然语言评测

选择一家服务，在自己的终端运行；Key 隐藏输入，不写入文件：

```sh
./run smoke --config config/deepseek.json
# 或：./run smoke --config config/qwen.json
./run bench --config config/deepseek.json --dataset datasets/natural-small --output results/deepseek-small.json
```

预设地址、模型 ID 和服务区域应与账号控制台核对。`smoke` 只发一个虚构 JSON 请求；`bench` 会产生实际 API 用量。每条命令在独立进程中运行，未通过环境变量提供 Key 时会重新询问。

## 交付结构

| 路径 | 用途 |
| --- | --- |
| `plugin/` | 可装入 AstrBot 的插件源码；元数据版本 v0.1.1 |
| `lab.py`、`run` | 独立学习、模拟回放入口 |
| `config/` | 规则、DeepSeek、Qwen、QQ 的配置与模板；不含 Key |
| `datasets/` | 完全虚构的消息、独立答案断言和生成参数 |
| `results/` | 本次实际运行的验证结果与日志 |
| `docs/` | 交付总结、教程、接口与评测说明、历史分析 |
| `tools/build_release.py` | 重建完整交付 ZIP、独立插件 ZIP、校验清单 |
| `tools/sandbox/` | 真实 AstrBot + OneBot 适配器 + 模拟 QQ 群的端到端沙盒；可接假模型或 DeepSeek／Qwen，见其 README |
| `SOURCE.json` | 原包 SHA-256 与改动范围 |

两个 ZIP 用途不同：完整交付包用于阅读、开发和测试；`astrbot_plugin_groupsecretary_v0.1.1.zip` 用于 AstrBot 的插件文件安装。不要把完整交付包当成插件上传。
