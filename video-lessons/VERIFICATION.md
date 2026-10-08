# 迁入验证（2026-10-08）

环境：Python 3.12、Pillow 12.3.0、NumPy 2.3.5、FFmpeg / ffprobe，系统 Noto Sans CJK 简体中文字体。

## 已完成

- `python -m unittest discover -s video-lessons/tests -v`：离线回归测试通过，无跳过；包含全部分镜边界、数对答案、字幕/时间轴、确定性帧、无声/有声真实编码、缓存失效及输入校验。
- `python -m py_compile video-lessons/lesson.py lessons/video/poem-coordinates/*.py`：通过。
- `git diff --check`：通过；文档相对链接检查通过。
- 14 个分镜、平移前/中/后关键帧生成成功，已人工查看分镜总览。
- 使用原源码包的 26 句缓存执行 `tts poem-coordinates --offline`：整段 WAV 与 SRT 和原版逐字节一致；时间轴除已安装 TTS 包版本元数据外一致。
- 使用新入口完整重绘：3036 帧、1920×1080、24 fps、126.500 秒，H.264 / yuv420p + AAC；ffprobe 检查通过，FFmpeg 全片解码无错误。
- 复用原版 MP3，从第 95 秒截取 1.25 秒，生成 30 帧短片成功；另测无声短片入口。

WAV SHA-256：`51033a473ceca25ac146afe3fc4e67bf2c0f7ae80591f1b9875d33cd14ab0715`

SRT SHA-256：`16b38a978c358e628e29a48507b21efa1df342d6cfc24c66348484656b68e628`

## 未完成或不在本次验证范围

- 未向在线 TTS 服务发送新的合成请求；真实缓存重建及失败/失效处理已离线测试。
- 未在 Windows / macOS 实机验证，字体自动查找在 Linux 环境验证。
- 未验证在线服务长期可用性，也未发布视频到外部托管服务。

以上是本次迁入的本地验证记录，不代表每台机器的依赖、字体或在线服务都相同；原源码包历史验证文件未作为本次通过证明。
