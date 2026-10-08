# 题目讲解视频（独立模块）

把已有的 **Pillow + NumPy 逐帧绘图、FFmpeg 编码、TTS 配音** 制作方式放进动课。这个模块可以单独运行，暂不依赖互动课件前端、网页播放器或后台。

首个示例是从原有源码包迁入的《古诗里的位置：有序数对与平移》，保留 14 段分镜、26 句旁白、数对高亮与整首诗平移动画。它是针对这一道题的完整示例，不是上传任意题图就能自动讲题的通用系统。

## 目录与许可

- `video-lessons/`：统一入口、依赖、测试与制作说明，通用工具代码沿用仓库 MIT 许可。
- `lessons/video/poem-coordinates/`：题目专属绘图与配音脚本、分镜、参考时间轴和字幕，全部沿用仓库 `lessons/` 的 CC BY-NC-SA 4.0 许可。
- 每道题的 `output/`：生成的视频、音频、缓存和预览，本地生成，不提交 Git。

这样保留了现有双许可证边界。今后可把真正跨题目通用的逻辑提取到本模块，题目内容和专属代码仍留在 `lessons/`。

## 安装

以下命令从仓库根目录执行。建议 Python 3.12（依赖要求 Python 3.11+）。先安装可在命令行找到的 FFmpeg 和 ffprobe，然后：

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell 改用：.venv\Scripts\Activate.ps1
python -m pip install -r video-lessons/requirements.txt
python video-lessons/lesson.py list
```

中文绘图还需要 **Noto Sans CJK SC Regular / Bold**。仓库不重复存放大型字体二进制。安装系统的 Noto CJK 字体，或从 [Noto CJK 官方仓库](https://github.com/notofonts/noto-cjk/tree/main/Sans) 获取两个文件：

- `NotoSansCJKsc-Regular.otf`
- `NotoSansCJKsc-Bold.otf`

把它们放进示例的 `assets/`，或对绘图命令追加 `--font-dir /你的字体目录`。也会查找 Linux 的 `/usr/share/fonts/opentype/noto`、用户字体目录及系统 macOS / Windows 字体目录；标准 Noto CJK TTC 使用简体中文字体面。第三方字体仍依其 SIL OFL 1.1 许可使用，详见示例素材说明。

## 无需联网，先看画面

```bash
python video-lessons/lesson.py validate poem-coordinates
python video-lessons/lesson.py preview poem-coordinates
python video-lessons/lesson.py frame poem-coordinates 95.0
python video-lessons/lesson.py render poem-coordinates --silent --duration 3 --output lessons/video/poem-coordinates/output/smoke.mp4
```

预览默认写入 `lessons/video/poem-coordinates/output/previews/`，包括 14 个分镜和一张总览图。`--silent` 明确输出无声视频；正常渲染缺少音频会报错，不会悄悄生成无声成品。

## 配音并生成完整视频

```bash
python -m pip install -r video-lessons/requirements-tts.txt
python video-lessons/lesson.py tts poem-coordinates
python video-lessons/lesson.py render poem-coordinates
```

TTS 使用 [edge-tts](https://github.com/rany2/edge-tts)，首次生成或修改旁白时会把分镜中的旁白文字、声音和语速参数发送到 Microsoft Edge 在线朗读服务。不要放入学生姓名、个人信息或未获许可的敏感内容。该在线服务的可用性不由本项目保证；脚本保留 TLS 证书验证，不携带密钥。声音是合成语音。

音频默认生成到本题 `output/`：逐句 MP3 缓存、24 kHz 单声道 WAV、整段 MP3、时间轴 JSON 和 SRT。渲染优先使用 `output/timeline.json`，否则用已提交的参考时间轴。修改了旁白但未重新生成匹配时间轴时，内容检查会拒绝渲染。

完整视频默认写入 `lessons/video/poem-coordinates/output/古诗数对与平移_讲题视频.mp4`。原示例为 1920×1080、24 fps，参考配音 126.476 秒，对齐帧边界后视频 126.500 秒。新配音时长会重新计算；不会固定截成原时长。

### 使用已有的原版配音

把本地 MP3 或 WAV 的路径交给渲染器，不需要重新调用 TTS。原版配音配合仓库的参考时间轴：

```bash
python video-lessons/lesson.py render poem-coordinates --audio /路径/古诗数对与平移_TTS配音.mp3 --timeline lessons/video/poem-coordinates/timeline.json
```

如果保留原源码包的完整 `audio/` 缓存，将其复制到本题 `output/audio/` 后，可完全离线重新拼接 WAV、MP3、时间轴与字幕：

```bash
python video-lessons/lesson.py tts poem-coordinates --offline
```

`--offline` 对缺失或与当前文字、声音、语速不一致的缓存直接报错。完整缓存重建无需安装 edge-tts，但仍需要 FFmpeg。

## 调试与扩展

- 短片检查：`render poem-coordinates --start 95 --duration 2 --audio /路径/配音.mp3`。视频和音频使用相同起点。
- 原生脚本参数：`python lessons/video/poem-coordinates/render_lesson.py --help`；TTS 同理。
- 更改题目内容：调整 `storyboard.json`，同步修改 `render_lesson.py` 的场景绘图、答案检查以及 `generate_tts.py` 的题目专属字幕转换。当前画布布局固定为 1920×1080。
- 新增独立题目：在 `lessons/video/<题目编号>/` 新建题目目录，并登记 `video-lessons/lesson.py` 的 `LESSONS`；遵循相同输出约定。
- 成品 MP4 建议通过单独的文件分发或后续 Release/对象存储发布；本次不自动发布视频或部署网站。

## 测试

```bash
python -m unittest discover -s video-lessons/tests -v
```

测试不调用在线 TTS。依赖齐全时会检查内容、时间轴、字幕格式、确定性绘图、缓存失效，以及实际短片编码。缺少 FFmpeg/字体时相关检查会明确跳过或失败；请查看实际测试报告。
