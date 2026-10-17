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

## 4. 交付前必过的自检（任一失败即不可交付）

```
python tests/selftest.py            # 功能与容器往返
python tests/test_gui.py            # 界面冒烟
python tests/test_ui_fit.py         # 真实屏幕自适应（主窗口 + 设置弹窗）
dist\MusicDecryptor.exe --check-qt  # 冻结后 Qt 可加载（无对话框）
dist\MusicDecryptor.exe --check-ui  # 冻结后界面/弹窗不越界
```

## 5. Git 与推送规则

- 远端仓库：`https://github.com/Nautilus233/Nautilus-s-Music-Decryptor.git`，分支 `main`（**公开仓库**）。
- **禁止自动/顺带推送**：只有当用户在当次消息里**明确要求推送**时，才允许 `git push`；
  用户已说明“以后的进展也是，但要在我说本次推送再推”。未经明确要求，连 commit 都不要主动做。
- 推送前必须跑通 §4 的自检。
- **禁止提交**：`dist/` 产物、`build/work*/` 缓存，以及任何**真实歌曲文件**
  （例如 `_testfile/` 内的 `.mgg`）。公开仓库严禁包含受版权保护的音频（详见 `.gitignore`）。

