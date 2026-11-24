## TheCompressor 1.1.0

**中文**

这版动默认值，顺手修了两处界面问题。

- 默认 CQ 从 21 改成 **23**：新装或删掉配置后启动就是 23，实测同一条 1080p 素材能省 66%。
- 推荐区间从 21～23 放宽到 **21～25**，滑块停在这段里头部一直显示「均衡推荐」，不再有单独的「偏体积」档。
- 24、25 的画质描述调成「中等 / 中等偏下」，和推荐区间的定位对上。
- 右上角显卡标注不再被截断：原来按字符数截，`NVIDIA GeForce RTX 4060 Ti · NVENC` 正好超一位，尾部的编码器名被吃掉；现在按像素宽度只截显卡名，`· NVENC / · QSV` 一定完整显示。
- 显卡标注下方加了署名「©灵魂调音师」，点击打开 B 站主页。
- 窗口外边距随 DPI 缩放，高缩放下不再贴边。
- 使用说明里的默认值、示例文件名后缀（`_cq23`）同步更新。

已保存的设置不受影响：程序会记住你上次拖到的值，想回默认就拖到 23。

**English**

Defaults plus two UI fixes.

- Default CQ is now **23** instead of 21; a fresh install (or a deleted config) starts at 23, which cut a 1080p test clip by 66%.
- The recommended band is widened from 21–23 to **21–25**: anywhere in that range reads "Balanced", and the separate "size-leaning" level is gone.
- Quality wording for 24/25 changed to match the recommended band.
- The GPU badge in the top-right is no longer cut off: it used to truncate by character count, and `NVIDIA GeForce RTX 4060 Ti · NVENC` was one character over, so the encoder name was dropped. It now truncates the card name by pixel width and always keeps `· NVENC / · QSV`.
- A credit link "©灵魂调音师" now sits under the GPU badge and opens the Bilibili channel.
- Window margins scale with DPI, so nothing hugs the edge at high scaling.
- The usage notes were updated with the new default and the `_cq23` output suffix.

Existing settings are preserved — the app remembers whatever value you last dragged to. Drag back to 23 to return to the default.
