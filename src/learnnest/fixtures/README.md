# 本地 `doctor` fixture

本目录的音频和图片不进入 Git，也不包含在构建产物中。`learnnest doctor` 为了实际验证 ASR 与 OCR provider，会在本机查找下面两个文件：

- `asr-smoke.wav`：应能转写出至少一个字幕片段。
- `ocr-smoke.png`：应能识别出至少一项 OCR 文本。

仅放入你已确认拥有使用和再分发权的本地测试媒体。公开仓刻意不提供、下载或生成它们；缺少文件时 `doctor` 会明确报错，而不会静默降级为非真实检查。
