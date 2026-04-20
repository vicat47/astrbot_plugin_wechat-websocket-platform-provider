# Changelog

## 0.1.5 - 2026-04-20
- 修复 `get_client()` 返回 `None` 的问题，现在返回适配器实例 `self`，使其他插件可通过 `event.bot` 正确获取 bot 实例。

## 0.1.4 - 2026-04-20
- 清理 `wechat_websocket_message_event.py` 中多余的 `self.message_obj` 赋值（基类已处理）。
- `self.bot` 改为通过 `adapter.get_client()` 获取，与官方适配器风格保持一致。
