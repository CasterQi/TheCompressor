## TheCompressor 1.0.0

**中文**

第一个公开版本。

- 显卡硬件编码：启动自动识别，NVIDIA 走 NVENC，Intel 核显 / Arc 走 QSV；自检失败会自动换到另一家可用的硬件编码器。
- 恒定质量压缩：CQ 16～30，21 ≈ `x265 -crf 21`，同一档位适用于所有素材，不用算码率。
- H.264 / HEVC，HEVC 同画质体积约小 30%。
- 批量拖入文件或文件夹（递归收集），实时显示百分比、倍速、剩余时间。
- 可选输出分辨率 4K / 2K / 1080p / 720p 等，等比缩放，竖屏同样适用。
- GPU 解码开关，不适用时自动回退。
- 单个 exe，内置 ffmpeg，不用装环境。

已知限制：AMD AMF 暂未支持；HDR 不做 SDR 色调映射；MP4 不写字幕轨；Intel H.264 为 8bit。

下载后直接双击 `TheCompressor.exe` 即可，首次启动会自解压内置 ffmpeg。

**English**

First public release.

- Hardware encoding out of the box: NVENC on NVIDIA, QSV on Intel iGPU or Arc, detected at startup; if the self-test fails it switches to the other available hardware encoder.
- Constant-quality mode: CQ 16–30, where 21 ≈ `x265 -crf 21`, so one setting works for every clip without bitrate math.
- H.264 / HEVC, with HEVC about 30% smaller at the same quality.
- Batch files or folders (recursive scan) with live percentage, speed multiplier and ETA.
- Optional output resolution 4K / 2K / 1080p / 720p, aspect ratio preserved, portrait included.
- GPU decoding toggle with automatic fallback.
- A single exe with ffmpeg bundled — nothing to install.

Known limitations: AMD AMF is not supported yet; no HDR→SDR tone mapping; MP4 carries no subtitle track; Intel H.264 is 8-bit only.

Just double-click `TheCompressor.exe`; the bundled ffmpeg unpacks on first launch.
