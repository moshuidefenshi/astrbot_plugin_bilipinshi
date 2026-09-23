# 评屎bot

让你的bot可以吃你搬的答辩视频回复简短评价，支持 B 站卡片转换。

## B 站卡片

插件会从 QQ Json 卡片、纯文本和原始消息中提取 BV、AV视频链接或 `b23.tv` 短链接，使用 `yt-dlp` 下载并由 FFmpeg 合并后分析。

## 依赖

安装插件依赖，并确保系统 PATH 中有 FFmpeg。模型需要支持图像输入；如果 B 站下载失败，默认发送标题和封面并记录错误日志，可在插件配置中关闭 `fallback_metadata_on_error`。

开启 `analyze_metadata_only` 后，B 站卡片只使用标题和封面进行评价，不下载完整视频，可节省下载流量和多模态模型 token。普通 QQ 视频仍按关键画面分析。

仅支持 QQ 个人号 `aiocqhttp` 适配器。
