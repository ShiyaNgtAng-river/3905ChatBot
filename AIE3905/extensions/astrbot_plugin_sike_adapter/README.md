# 伺客 HTTP 适配器

让 AstrBot 按客户“数字员工”的 IM 协议收发消息（伺客坐席，企业微信）。协议的另一端参考实现是 `digital-employee-talk`（网页联调程序），字段说明见它的 `docs/downstream-integration.md`。

## 协议

- 收单：`POST /api/v1/im/messages`，九个字符串字段。成功回 HTTP 202 `{"messageNo", "accepted": true, "duplicate"}`；字段不对回 400。同一 `messageId` 重投只确认，不重复处理。
- 回复：`POST <reply_url>`，七个字段，请求头 `X-Digital-Employee-Request-No` 防重。群聊 `roomId` 为群 ID，`receiver` 为触发回复的成员；单聊 `roomId` 为 JSON null。网络错误或 5xx 用同一编号重试 3 次。
- 只回文字。回复里的图片等媒体会丢掉，@ 写成文字 `@名字`。
- 协议没有“@了谁”字段。消息文字里出现 `@` 加 `bot_names` 中的名字（或坐席 userId）时，适配器把它转成 At 机器人，并从正文里去掉。群记插件因此仍能读到全部群消息，只在被 @ 时回复。

## 配置（平台类型 `sike_http`）

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `listen_host` / `listen_port` | `127.0.0.1` / `18080` | 收单监听地址 |
| `inbound_path` | `/api/v1/im/messages` | 收单路径 |
| `reply_url` | `http://127.0.0.1:3000/api/v1/im/replies` | 回复回调地址 |
| `bot_account_id` / `bot_user_id` | `LIRUNLIN919` / `sales-bot-01` | 坐席伺客账号、坐席企微 userId；收单时校验 |
| `bot_names` | `数字员工` | 群里 @ 机器人用的名字，逗号分隔 |
| `inbound_token` | 空 | 设置后要求 `Authorization: Bearer <token>`；参考网页不发送令牌，只在本机监听时留空 |

## 本机演示

1. 启动隔离的 AstrBot（独立数据目录和端口，不连 QQ，不碰线上数据）：
   `<AstrBot>/.venv/bin/python tools/sike_demo.py --astrbot <AstrBot> --admins 陈经理`
2. 启动网页：在 `digital-employee-talk` 目录运行
   `DOWNSTREAM_URL=http://127.0.0.1:18080/api/v1/im/messages npm start`
3. 网页里建群或加入群，群 ID 用 `room-demo`（与 `--room` 一致），然后 `@数字员工` 提问。
   也可以运行 `python3 tools/sike_demo_chat.py`，它用三个虚构成员发一段配件订单对话，并问三个问题。

## 测试

`python3 -m unittest discover -s tests`（只测协议部分，不需要 AstrBot）。
