# AGENTS.md — 音乐解密工具 · 项目约束（工程红线）

> 本文件约束本仓库（`C:\Users\Nautilus\Desktop\Music Decryptor`）内的一切改动。
> 用户已明确三条不可协商的要求：**零依赖**、**轻量化**、**兼容性第一**。
> 与用户临时指令冲突时，先指出冲突并确认，不得默认绕过。

## 1. 零依赖（运行时）

- 运行时依赖**只允许**两种：
  - `PySide6` —— 图形界面；
  - `mutagen` —— 写标签/封面，**可选**；未安装时必须优雅降级（仍能完成解密）。
- 音频加解密、网络、去重、配置、打包等逻辑**必须只用 Python 标准库或本仓库内的纯 Python 代码**。
  已自研并用测试固定：AES（`crypto/aes.py`）、TEA（`crypto/tea.py`）、NCM/QMC 解码器、`urllib` 网络层。
- **禁止**引入原生 / 二进制运行时依赖，例如但不限于：`pycryptodome`、`requests`、`numpy`、
  `ffmpeg`、`chromaprint`、`librosa`、非标准库的数据库驱动。标准库 `sqlite3`、`hashlib`、`json` 可用。
- **例外（2026-10-18 用户明确同意）**：转码引擎复用 **PySide6 自带的 FFmpeg 库**（`avcodec/avformat/avutil/swresample`，LGPL，运行时 ctypes 动态加载）——不新增外部依赖、不打包 `ffmpeg.exe`；引擎缺失时**静默回退保留原格式**，其余功能不受影响。权衡与被否决方案见 `DESIGN.md` §3.4。
- **用户凭证**（`qqmusic_key` / `qqmusic_uin`）：仅保存在本机 `settings.json`，**日志绝不输出**，只用于官方 `GetEVkey` 端点；留空即关闭该功能。禁止内存扫描 / 浏览器 cookie 抓取等“凭证窃取”式实现。
- 新增**任何**运行时依赖前，必须先取得用户明确同意，并在 `DESIGN.md` 记录理由与被否决的替代方案。

## 2. 轻量化

- 单文件 exe 体积不得显著增长（当前基线 ≈ 46 MB，其中 Qt 三个核心 DLL ≈ 25 MB）。
- 新增功能优先复用已有 Qt 模块；不得为一个功能引入新的 Qt 子模块（`build/*.spec` 的 `excludes` 只增不减，除非有明确理由）。
- 构建期依赖（`pillow`、`pyinstaller`、`python-docx`）不受限，但**不得进入产物**。
- 禁止启用 UPX（显著抬高杀软误报率）。

## 3. 兼容性（最高优先级）

- 任何改动都必须通过**发布验收清单**（见 `DESIGN.md` 附录 D）。
- **不得**把正确性建立在“打包机的 PATH / 环境恰好可用”之上；打包后必须审计 bundle 内 DLL 的导入/导出自洽性
  （历史事故：`PATH` 上的 Poppler `icuuc.dll` 被误打包，导致冻结版 `import PySide6.QtCore` 直接失败）。
- 界面尺寸/位置**一律**走 `ui/geometry.py`（工作区 + 窗口边框感知）；**禁止**写死 `resize()`。
- 离线解码核心必须独立于网络与任何第三方服务；联网仅用于“备用下载”与可选封面。
- 遇到无法确定的信息（模糊文字、未知格式、损坏容器）**不得猜测**：原样保留并给出可读提示。

## 4. 置信度操作必须静默、自动（用户要求）

> 用户明确：**比对时长等为了置信度的操作必须静默完成，不得要求用户提供参考时长**；
> 软件要保持**简单易上手、快速稳定**，不得额外增加使用难度。

- 一切置信度手段（版本过滤、歌名硬匹配、跨音源择优、时长核验…）**全部自动完成**，
  只在日志里说明做了什么。
- **禁止**为实现置信度而新增任何用户输入：不要"请用户确认版本"、不要"请用户提供参考时长"、
  不要新增需要用户配置的阈值/开关。
- 结果提示使用**陈述式**（如"完成（时长未比对）"），不得写成要求用户行动的问句或指令。
- 置信度操作要有**成本上限**：探测类请求要有数量上限与更短的超时，且失败即跳过，不得拖慢主流程。
- 若置信度无法完成，**照常产出结果并如实标注**，而不是阻塞、弹窗或反复追问。
- **唯一例外（用户 2026-10-22 明确要求并要求落地）**：`fast_mode`「极速模式」开关 ——
  它是**性能偏好**（速度 ↔ 精确度），不是置信度输入：默认 **关**（=精确优先），
  不要求用户提供参考时长、不新增阈值、不改变任何门槛写法；开启时只跳过"多源交叉核验"，
  门槛/时长下限/完整度/下载校验全部照常。实现与取舍见 `DESIGN.md` §3.9.2。

## 5. 交付前必过的自检（任一失败即不可交付）

```
python tests/selftest.py                # 功能与容器往返（离线用例）
python tests/test_gui.py                # 界面冒烟
python tests/test_ui_fit.py             # 真实屏幕自适应（主窗口 + 设置弹窗）
python tests/test_robustness.py         # 边界 / 负面 / fuzz / 故障注入 / 内存
python tests/test_monkey_ui.py          # 猴子测试：随机乱点全部控件
dist\MusicDecryptor.exe --check-qt      # 冻结后 Qt 可加载（无对话框）
dist\MusicDecryptor.exe --check-ui      # 冻结后界面/弹窗不越界
dist\MusicDecryptor.exe --check-transcode # 冻结后转码引擎能加载并转出文件（体积裁剪后必须）
dist\MusicDecryptor.exe --check-worker  # 冻结后任务线程生命周期（跑任务→关窗→退出不崩）
dist\MusicDecryptor.exe --check-jp      # 冻结后日语/纠错内置表可加载（P4，2026-10-22 新增）
dist\MusicDecryptor.exe --sources-report # 音源清单与台账健康（信息性，但须 exit 0）
```

`--check-transcode` 是 2026-10-22 补的：打包裁剪掉了 Qt 的 Qml/Quick/PDF/OpenGL/Svg
等未使用模块 **以及 opengl32sw.dll**，并把重复的 FFmpeg 四件套去重 —— 这条门禁专门证明
"裁剪之后转码引擎在冻结产物里仍然能加载、能转出文件"（源码能跑 ≠ exe 能跑）。

后三条是 2026-10-20 补上的，**各自都抓到过真实缺陷**，不是凑数：

- `test_robustness.py` → 抓到 NCM/KWM **静默产出打不开的文件**、台账 `_connect()` 异常分支的
  **文件句柄泄漏**、以及手改坏的 `settings.json` 会把界面炸在远处；
- `test_monkey_ui.py` + `--check-worker` → 抓到 **QThread 生命周期导致的退出崩溃**
  （`0xC0000409`：`completed` 在 `run()` 内发出时就丢掉最后引用 + 关窗只 `cancel()` 不等）。
  冻结产物也跑这条，是因为"源码能跑 ≠ exe 能跑"（见 DESIGN §5.6）。

深度浸泡（`test_robustness.py --soak`、`test_monkey_ui.py --iters 1500 --rounds 5`）与换随机种子
（`--seed`）按需跑，不进常规门禁。

## 6. Git 与推送规则

- 远端仓库：`https://github.com/Nautilus233/Nautilus-s-Music-Decryptor.git`，分支 `main`（**公开仓库**）。
- **禁止自动/顺带推送**：只有当用户在当次消息里**明确要求推送**时，才允许 `git push`；
  用户已说明“以后的进展也是，但要在我说本次推送再推”。未经明确要求，连 commit 都不要主动做。
- 推送前必须跑通 §5 的**全部**自检（含上面那条新增门禁）。
- **禁止提交**：`dist/` 产物、`build/work*/` 缓存，以及任何**真实歌曲文件**
  （例如 `_testfile/` 内的 `.mgg`）。公开仓库严禁包含受版权保护的音频（详见 `.gitignore`）。
- 提交前扫一眼 `git status`：**仓库根不得出现测试垃圾目录**（猴子测试会把"输出目录"填成
  `设置` / `🎵🎤` 这类脏值；`tests/test_monkey_ui.py` 已用 `os.chdir` 隔离并自带"零新增"自检，
  发现残留就说明隔离失效，先修测试再提交）。

## 7. Release 发布规则（用户 2026-10-21 明确）

> 用户原话：**"以后也这样，这才是软件的预期发布方式，按需设置 pre-release 和 release 而并非只有一个 release。"**

- **多个 `release`（正式版）可以并存**，这是预期形态：新正式版发布后，**不要**把上一个稳定版
  改成 pre-release —— 它是并列的稳定回退点（例：`v1.0.23` 与 `v1.0.22` 同为 release）。
- **何时用 pre-release**：`-dev*` 迭代版、或明显还在迭代/已知有缺口、**不打算让人当稳定版使用**的版本。
  何时用 release：走完 §5 全部门禁、可作为稳定回退点的版本。
- **Latest 归属**：GitHub 按发布时间取最新的非 pre-release，所以新正式版发完 Latest 自然归它；
  改任何版本的 `prerelease` 标记后，都要复核 `GET /releases/latest` 仍指向预期版本。
- **改动范围**：只改 `prerelease` / `draft` 时**不要**重建 tag、不要动资产（除非用户要求刷新资产）。
- 发布/改标记属于对外可见操作：**仍受"当次消息明确要求才做"的约束**（§6）。

