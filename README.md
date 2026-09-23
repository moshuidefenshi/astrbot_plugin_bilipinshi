# 评屎bot

让你的 bot 可以吃你搬的答辩视频并回复简短评价。

## 功能

- 监听 QQ 个人号的群聊和私聊消息。
- 收到普通 QQ 视频后，抽取关键画面交给当前多模态模型分析。
- 回复评价文字，并单独回发被分析的视频。
- 识别 QQ Json 消息中的 B 站视频卡片，支持 BV、AV视频链接和 `b23.tv` 短链接。
- B 站卡片默认下载视频、抽取关键画面并分析，再分别发送评价和视频。
- 可切换为只分析 B 站卡片的标题和封面，以节省 token。

## 安装

将插件目录放入 AstrBot 的 `data/plugins` 目录，然后安装依赖：

```bash
pip install -r requirements.txt
```

插件使用 `imageio-ffmpeg` 自动提供 FFmpeg，不需要手动安装系统 FFmpeg。B 站视频下载使用 `yt-dlp`。当前仅支持 QQ 个人号 `aiocqhttp` 适配器。

模型需要支持图片输入。插件会把视频关键帧或 B 站封面作为图片发送给当前会话使用的模型。

## 配置

在插件配置中可以调整：

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `analyze_metadata_only` | 只分析 B 站卡片标题和封面，不下载视频 | `false` |
| `review_tendency` | 评论倾向滑块，0 偏差评，50 中立，100 偏好评 | `50` |
| `video_max_frames` | 普通视频最多抽取的关键帧数量 | `6` |
| `http_timeout` | 网络请求超时时间，单位为秒 | `30` |
| `system_prompt` | 视频评价使用的系统提示词 | 内置短评提示词 |

默认提示词为：

```text
你是一个视频评价助手。用一句话评价视频（1-10个字，可以用🐛表示不好看）
```

## 处理方式

### 普通 QQ 视频

插件读取视频文件或 URL，抽取关键画面，调用当前模型生成短评，然后分别发送短评和原视频。

### B 站卡片

关闭 `analyze_metadata_only` 时，插件通过 `yt-dlp` 下载视频，使用 `imageio-ffmpeg` 抽帧后分析，并分别发送短评和视频。

开启 `analyze_metadata_only` 时，插件只请求 B 站公开接口获取标题和封面，不下载完整视频，适合节省流量和 token。

## 故障排查

- 确认 AstrBot 已配置支持图片输入的模型。
- 确认插件依赖已安装：`pip install -r requirements.txt`。
- B 站视频下载失败时，查看 AstrBot 日志中的 `B站卡片评价失败` 和 `yt-dlp` 错误信息。
- 若只想评价标题和封面，将 `analyze_metadata_only` 设置为 `true`。
