# 第一个真实群的接入验收

建议先在自有飞书测试群完成。下列检查是上线前要实际执行的项目，不是此交付已经完成的测试结果。

| 检查项 | 操作 | 通过条件 |
| --- | --- | --- |
| 群边界 | 配置一个测试群，另用未启用群发送消息 | 仅启用群记录；其他群默认行为不受本插件影响 |
| 全量消息 | 连续发三条不 @ 的普通讨论 | 三条均进入审核页；机器人没有逐条回复 |
| 稳定身份 | 修改群名片后继续说话 | 用户 ID 不变，显示名随平台更新 |
| 唤醒 | @ 机器人发问 | 只有一条负责回复的流程，没有默认模型重复回答 |
| 引用 | A 提议，负责人引用并回复“可以” | `reply_to` 正确；确认依赖对应原消息 |
| 并行事项 | 两个成员交叉讨论不同安排 | 分别关联到正确事项；含糊短回复不强行确认 |
| 更新 | 确认时间后改期 | 当前答案使用新时间；回溯能找到旧版本 |
| 确认权限 | 非负责人取消、负责人再确认 | 前者待确认，后者才改变正式状态 |
| 撤回 | 撤回确认新时间的原消息 | 适配器桥接到撤回入口；不再引用；状态不擅自恢复旧时间 |
| 编辑 | 平台编辑已有消息 | 转为新的 `edit` 修订，旧证据失效；不可静默覆盖 |
| 附件 | 发送只有截图的公告 | 显示附件未处理，不声称已读懂图片 |
| 断线恢复 | 中断连接后恢复 | 明确平台能否补投；重复投递不重复记事、不重复回复 |
| 原文定位 | 从答案核对来源 | 稳定消息 ID 可定位；无深链接的平台使用 `/原文` 或审核页 |
| 隐私退出 | 成员发送 `/别记我` | 本插件内容及依赖被清理；后续不入账；其他副本另行确认 |
| 群隔离 | Web 令牌尝试访问无权限群 | 返回拒绝，模型未获得该群数据 |
| 定时群报 | 临时配置接近当前时间的群报 | 在正确群发送；记录已发送日期；检查重启边界 |
| 插件重载 | 模型处理期间重载插件 | 不留下重复后台任务；待处理消息可继续 |

## 适配器桥接契约

普通消息由 `main.py` 标准化，核心输入为 `secretary.types.Message`。未交付的撤回、编辑、历史或引用关系，不能由核心补造。

独立撤回回调应调用：

```python
await plugin.ingest_recall(platform_id, group_id, native_message_id)
```

编辑应在可信适配层构造：

```python
Message(group="pilot", sender="u-1", text="修改后的原文",
        at="2026-09-27T10:30:00+08:00", native_id="m-12",
        kind="edit", revision="平台给出的修订号或稳定更新时间")
```

`/api/ingest` 是有管理员权限的开发 / 回放入口，不是公开 Webhook。若企业通过网络推送事件，需另建签名验证、重放防护和平台身份映射层，不要把管理令牌给群成员。

## 本次核对的 AstrBot 源码入口

- [项目版本与 Python 要求](https://github.com/AstrBotDevs/AstrBot/blob/master/pyproject.toml)：核对时为 4.28.1，Python 3.12+。
- [AstrMessageEvent](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/platform/astr_message_event.py)：平台 ID、发送者、唤醒状态与 `should_call_llm`。
- [Context](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/star/context.py)：`llm_generate` 与 `send_message`。
- [Star 生命周期](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/star/base.py)：初始化、关闭。
- [MessageChain](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/message/message_event_result.py)：构造发送内容。
- [消息监听文档](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)。

核对源码能确认调用形状，不能代替实际平台权限、SDK 和网络的端到端测试。
