# 第三方组件与许可

LuHui音乐播放器 以 **GNU General Public License v3.0** 发布（见 `LICENSE`）。
下列组件由本项目使用或随便携版分发，其许可如下。版本号是开发机实测值，
不是强制锁定版本。

## 运行时 / 库

| 组件 | 版本（开发机） | 许可 | 说明 |
|---|---|---|---|
| Python | 3.12.3 | PSF License | 本地音频服务解释器。 |
| NumPy | 2.4.6 | BSD 3-Clause | PCM 缓冲、DSP、频谱。 |
| SciPy | 1.17.0 | BSD 3-Clause | EQ 双二阶滤波与重采样辅助。 |
| mutagen | 1.48.1 | GPL 2.0-or-later | 标签/封面/内嵌歌词读取。 |
| Pillow | 12.2.0 | MIT-CMU | 封面缩放与缓存。 |
| PyAudioWPatch | 0.2.12.9 | MIT | WASAPI 共享/独占设备枚举与 PortAudio 回调。 |
| imageio-ffmpeg | 0.6.0 | BSD 2-Clause | 提供 FFmpeg 运行时，见下。 |

## 原生外壳

| 组件 | 版本（开发机） | 许可 | 说明 |
|---|---|---|---|
| .NET 8 | 8.0.x | MIT | WinUI 3 应用运行时。 |
| Windows App SDK | 1.8.260921001 | MIT | WinUI 3 控件、窗口与生命周期。 |
| Microsoft.Windows.SDK.BuildTools | 10.0.26100.9457 | MIT | 构建工具。 |

## 旧版 Qt 外壳（可选）

| 组件 | 版本（开发机） | 许可 | 说明 |
|---|---|---|---|
| PyQt5 | 5.15.10 | GPL v3（Riverbank Computing） | 旧外壳。GPL 版 PyQt5 要求衍生作品同样以 GPL 发布，本项目因此选择 GPLv3。 |
| Qt 5 | 5.15.2 | LGPL v3 / GPL v3（The Qt Company） | PyQt5 依赖的 Qt 运行库。 |
| PyQtWebEngine | 5.15.6 | GPL v3 | 旧外壳内嵌 Chromium 87。 |

## 开发工具（可选）

| 组件 | 许可 | 说明 |
|---|---|---|
| pywin32 | PSF License | `native/run_native_hidden.py` 的无前台审计。 |
| psutil | BSD 3-Clause | 同上，进程树采样。 |

## FFmpeg

本仓库**不提交** FFmpeg 可执行文件。`imageio-ffmpeg` 在安装时提供一份运行时构建，
其许可取决于该构建的编译选项：LGPL 2.1+ 或 GPL 2+。分发打包产物前请核对该构建的
`--enable-gpl` 等选项，并随包附带对应许可文本与源码获取方式。

## 字体与美术

界面使用 Windows 系统字体，不随仓库分发字体文件。
`native/SaltPlayer.Native/Assets` 与 `web/favicon.svg` 中的图形为本项目自制。
