# Squad 间瞄火力助手（Windows 桌面版）

> AI 识图 + 间瞄计算：游戏内按一个热键，读出 CURRENT GRID 坐标，算出迫击炮/火箭巢的密位与方位角，结果显示在永远置顶的半透明浮窗里。

本目录同时提供**可直接运行的成品**与**全部源码**，便于公开审查。

## 下载（免安装单文件）

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| [`Squad.exe`](https://github.com/copperbookworm/Yehorivka-Passability/releases/latest/download/Squad.exe) | 107,721,736 字节（约 102.7 MB） | `80382EDFF30F86578F7D13A78A94BF17B96428501CBF4D560F2D563741E3BC65` |

- Windows 10 / 11 x64，双击即用；首次运行在同目录生成 `config.json` 与 `run.log`。
- 已用自签名证书签名，SmartScreen 仍会提示未知发布者 → 「更多信息 → 仍要运行」。
- 校验：`Get-FileHash .\Squad.exe -Algorithm SHA256`

## 源码结构

| 路径 | 作用 |
| --- | --- |
| `main.py` | 全部界面与业务逻辑：地图视图、面板、热键线程、截图识别流程、三点外心、剪贴板、单实例、打包入口 |
| `core.py` | 弹道内核：网格坐标编解码、速度（含减速段）、高低角双解、飞行时间、散布；与 SquadCalc 46.1.0 逐条对齐 |
| `mapsdata.py` | 地图数据：1024×1024 高程网格双线性插值、格号互转、底图 |
| `vision.py` | DeepSeek `deepseek-flash` 识图：截图 → base64 → 读 CURRENT GRID → 正则归一化 |
| `overlay.py` | 置顶半透明浮窗：状态行 + 阵地/目标/密位/方位 四行，可拖动、不抢焦点 |
| `paths.py` | 资源/配置路径（源码运行 vs PyInstaller 单文件） |
| `data/maps.json` | 25 张官方地图的边界、网格跨度、高程缩放 |
| `data/weapons.json` | 26 组武器/弹种的弹道参数（初速、MOA、射界、偏移、减速、寿命） |
| `data/maps/*.jpg` | 25 张 2048² 底图 |
| `data/maps/*_elev.png` | 25 张 1024² 16-bit 高程栅格 |
| `tools/build_data.py` | 从 Squad 地图源数据重建 `data/`（底图 + 高程 + 元数据） |
| `docs/USAGE.md` | 完整使用/开发/打包/签名说明 |
| `build/version_info.txt`、`build/app.spec` | PyInstaller 版本资源与打包描述 |
| `app.ico` | 程序图标 |

## 从源码运行

```bat
pip install -r requirements.txt
python main.py
```

打包成单文件（需要 `pyinstaller`）：

```bat
cd build
python -m PyInstaller --noconfirm --clean app.spec
```

## 功能要点

- **25 张官方地图 × 26 组武器/弹种**，弹道与 [SquadCalc](https://github.com/sh4rkman/SquadCalc) 46.1.0 对齐（3,220 条解的比对，最大误差 8.3e-14 度）。
- **四个可自定义热键**：`F1` 快速定位（截 1 次）· `F2` 准确定位（连截 3 次取三点外心）· `F3` 清除未完成的坐标（已算好的目标保留）· `F4` 浮窗显隐。
- **浮窗**：半透明、文字不透明、可拖动、不抢焦点；空闲时进程 CPU 0.00%。
- **算完自动复制**目标格号到剪贴板（Win32 API + `OleFlushClipboard`，避免 Qt 偶发 `0x800401d0`）。
- **区域预设**：常用截图框可保存/重命名/删除/套用。

## 已知限制

- 游戏请用「无边框窗口」，独占全屏会盖住浮窗。
- `F1` 常被 Snipaste 等截图工具占用；错误码 1409 = 热键被占用。
- 只作估算：高程网格约 6 m 精度，弹道参数来自 SquadCalc 的经验拟合（非 UE 物理），首发命中仍需校射。
- AI 可能把 `I` 读成 `L`，程序会提醒核对。

## 许可

弹道公式与武器数据取自 [SquadCalc](https://github.com/sh4rkman/SquadCalc)（MIT-NC，作者 sh4rkman）。
本程序同样**仅限非商业用途**，详见 [`LICENSE.md`](LICENSE.md) 与 [`NOTICE.md`](NOTICE.md)。
