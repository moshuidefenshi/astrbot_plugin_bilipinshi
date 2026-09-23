# 评屎bot

让你的bot可以吃你搬的答辩视频回复简短评价，支持 B 站卡片转换。

B 站卡片在关闭 `analyze_metadata_only` 时会使用 yt-dlp 下载视频、抽取关键帧分析，并分别回复评价和视频；开启时只读取标题和封面评价，以节省 token。普通 QQ 视频始终抽取少量关键帧，FFmpeg 由 `imageio-ffmpeg` 依赖自动提供，不需要手动安装系统 FFmpeg。

`analyze_metadata_only` 控制 B 站卡片是否只分析标题和封面。

## B 站卡片

插件会从 QQ Json 卡片中提取 BV、AV视频链接或 `b23.tv` 短链接，再请求 B 站公开接口获取标题和封面。

## 依赖

安装插件依赖即可使用。模型需要支持图像输入。

`review_tendency` 是 0 到 100 的滑块：0 偏差评，50 中立，100 偏好评。默认提示词要求 AI 用一句 1 到 10 个字的话评价，也可以在 `system_prompt` 中修改。

仅支持 QQ 个人号 `aiocqhttp` 适配器。
