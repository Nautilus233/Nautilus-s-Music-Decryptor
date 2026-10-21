# 音乐解密工具 · 设计文档

> **字体规范声明（遵循 AGENTS.md）**
> 本文档为中文交付文档：正文使用 **微软雅黑**，标题使用 **黑体**，代码 / JSON 等结构化文本使用 **Consolas**。
> 除上述字体外不使用任何其它字体，也不使用非 GBK 兼容字体。
> 说明：本文档不含任何图像识别内容；若出现无法辨认的模糊文字，将原样保留并另行向用户确认，绝不猜测。

> **合规声明**
> 本工具仅用于**个人本地研究**：解密*用户本机已存在*的加密音频、或从其自行的备份中恢复。
> 项目**不内置、不携带、不预置任何受版权保护的音频内容**，所有密钥与算法均来自公开的开源逆向实现（`unlock-music`、`qmcdump` 等）。
> 请遵守当地法律与各平台用户协议；请勿传播解密后仍受版权保护的作品。

---

## 0. 目标与范围

- **离线优先**：NCM、QMC0/QMC3/QMCflac/QMCogg 等常见加密格式，**完全离线**解密，不需要联网、不依赖任何付费 API。
- **自动降级**：遇到必须联网 / 硬件绑定的新格式（新版 QQ 音乐 `STag` 类文件），自动标记“需联网破解”，并**立即转入备用搜索-下载安全音源**流程。
- **全程容错**：所有网络操作有超时与重试；解码失败不崩不卡，返回可读错误；全部途径失败才抛出明确提示。
- **单文件发布**：打包为一个带自定义图标的独立 `.exe`（Windows 10+）。

### 0.1 设计红线（用户约束，不可协商）

用户在 v1.0.3 之后明确：**除了使用体验与效果本身，最看重的是兼容性**；并且要求软件**始终零依赖、轻量化**。
本节把这三条固化成可核对的工程约束，`AGENTS.md` 为强制版本。

**① 零依赖（运行时）——当前实际清单**

- 仅两个运行时依赖：`PySide6`（界面）、`mutagen`（可选；未安装时仍能完成解密，只是不写标签/封面）。
- 其余全部为**标准库或本仓库纯 Python 代码**：AES、TEA、NCM/QMC 解码器、`urllib` 网络层、`hashlib`、`json`。
  （这也是当初刻意不用 `pycryptodome` / `requests` 的原因。）
- 禁止原生/二进制运行时依赖；新增任何运行时依赖须先取得用户同意。

**② 轻量化——当前实际构成**

- 单文件 exe 实测 **45.9 MB**；其中 Qt 三个核心 DLL 约占 **25 MB**（`Qt6Core` 10.0 + `Qt6Gui` 9.2 + `Qt6Widgets` 6.2）。
- 也就是说：**体积的大头是 Qt，不是本项目代码**。因此“再轻量”只可能来自取舍界面框架，而不是继续裁剪业务代码。

**③ 兼容性——第一优先级**

- 交付以 `DESIGN.md` 附录 D 的**发布验收清单**为准，而不是“在我这台机器上能跑”。
- 三个已发生并修复的兼容性事故作为前车之鉴：
  1. 打包机 `PATH` 上的同名 `icuuc.dll`（Poppler）被误打包 → 冻结版 Qt 无法导入（§5.6）；
  2. 写死 `resize()` 未计入窗口边框 → 200% 缩放下底部按钮被截断（§4.3）；
  3. 回调里用 emoji 打印在 GBK 控制台抛 `UnicodeEncodeError` → 拖垮整批任务（§1.5）。

---

## 1. 整体架构设计

### 1.1 模块划分

分四层，层间只通过明确定义的数据结构通信，便于单独测试与替换：

- **界面层 `ui/`**
  - `main_window.py`：主窗口（列表、进度、日志、拖拽）。
  - `settings_dialog.py`：设置对话框（音源开关、网络参数、行为开关）。
  - `worker.py`：`QThread` 后台任务，把流水线的回调转成 Qt 信号。
  - `theme.py`：深色 QSS 主题。
- **编排层 `mmusic/pipeline.py`**
  - 每个文件的决策中枢：`识别 → 离线解密 → 失败则备用下载 → 写盘 + 打标签`。
  - 唯一对外入口：`Pipeline.process_file()` / `Pipeline.process_many()`。
- **能力层（无界面、可独立复用）**
  - `mmusic/sniff.py`：格式识别（头部魔数 + 扩展名 + 尾部特征）。
  - `mmusic/crypto/`：`aes.py`（纯 Python AES-ECB）、`tea.py`（TEA + 腾讯 TEA-CBC）、`ncm.py`、`qmc.py`。
  - `mmusic/search/`：备用音源适配器（`kuwo` / `kugou` / `migu` / `aggregator` / `local` 等）与注册表。
  - `mmusic/downloader.py`：下载结果校验（文件头、码率、大小）。
  - `mmusic/metadata.py`： mutagen 读写标签与封面（缺失时优雅降级）。
  - `mmusic/net.py`：基于 `urllib` 的 HTTP 客户端（超时 / 重试 / gzip / 流式下载）。
  - `mmusic/hashcache.py`：SHA-256 去重台账。
  - `mmusic/match.py`：歌名归一化与最佳匹配打分。
  - `mmusic/lang.py`：华语歌预匹配（汉字 + 歌手名单），决定备用链路的信源排序。
  - `mmusic/config.py` / `paths.py` / `model.py` / `errors.py` / `utils.py` / `constants.py`。

### 1.2 目录结构

```text
Music Decryptor/
├─ main.py                     # 入口：GUI / --cli / --selftest
├─ requirements.txt
├─ README.md                   # 快速上手
├─ DESIGN.md                   # 本文档
├─ mmusic/                     # 核心能力层（无界面）
│  ├─ pipeline.py              # 编排中枢
│  ├─ sniff.py  constants.py  model.py  errors.py  utils.py
│  ├─ net.py  downloader.py  hashcache.py  metadata.py  match.py
│  ├─ config.py  paths.py
│  ├─ crypto/  aes.py  tea.py  ncm.py  qmc.py
└─ search/  base.py  pack.py  registry.py  index.py  fusion.py
             kuwo.py  kugou.py  aggregator.py  local.py
├─ ui/                         # PySide6 界面层
│  ├─ main_window.py  settings_dialog.py  worker.py  theme.py
├─ assets/                     # 图标与图标生成脚本
│  ├─ icon.ico  icon.png  make_icon.py
├─ build/                      # 打包
│  ├─ music_decryptor.spec  music_decryptor_console.spec  build.ps1
├─ tools/
│  └─ make_docx.py             # 生成本设计文档的 DOCX 版本
└─ tests/
   ├─ selftest.py  test_gui.py  fixtures.py
```

### 1.3 数据流

```text
输入文件 ──► sniff(格式识别)
                │
                ├─ plain(已是明文音频) ──────────────► 写出 + 打标签
                ├─ ncm ──► NcmDecoder ──► AudioResult ─► 写出 + 打标签
                ├─ qmc ──► QmcDecoder ──► AudioResult ─► 写出 + 打标签
                │              │
                │              └─ STag/无密钥 ─► NeedsOnlineError ─┐
                └─ unknown ──────────────────────────────────────┤
                                                                 ▼
  备用下载： 歌名/歌手解析 ─► 各音源 search ─► rank 打分 ─► resolve 直链
             ─► 流式下载 ─► validate(文件头/码率/大小) ─► 写出 + 打标签
             （全部失败 ─► SearchNoResultError）
                                                                 │
                                    JobResult(状态/输出路径/说明) ┘
```

关键数据结构：

- `AudioResult`：`audio: bytes` + `ext` + `title/artist/album/cover_url/cover` + 原始 `meta`。
- `Track`：一条搜索结果（`source/title/artist/duration/bitrate/ext/url/raw/score`）。
- `JobResult`：单个文件的结局（`status: decoded|downloaded|copied|skipped|failed`、`out_path`、`message`、`detail`）。

### 1.4 控制流（决策状态机）

```text
对于每个文件：
 1. 计算 SHA-256（可选）→ 命中缓存且产物仍在 → 直接 skipped
 2. sniff 识别
    · plain  → 复制（copied）
    · ncm    → 解密；成功→decoded；异常→第 3 步
    · qmc    → 若 needs_online →第 3 步；否则解密；成功→decoded；异常→第 3 步
    · unknown→ 第 3 步
 3. 备用下载（enable_fallback 打开时）
    对每个音源（按配置顺序）：
       search → 打分排序 → 取前 3 且 score≥0.34
          resolve → download → validate
             校验通过 → 写出（downloaded），结束
             任一环节失败 → 记录原因，尝试下一个候选
    所有音源/候选耗尽：
       · 一个候选都没进入（全部低于阈值/无结果）→ “搜索无结果，请检查网络或歌曲名”
       · 有候选但都没通过 → “候选音源均不可用（无版权/校验失败/反爬），请更换关键词或手动补全直链”
 4. 写盘 + 打标签（标题/艺术家/专辑/封面，全部**直接写入音频文件**）
 5. 记录缓存；可选删除原加密文件
```

单个文件的最大隔离原则：`process_file` 的任何“预期异常”都被转成 `JobResult`；`process_many` 外层再兜底一次 `except`，**一个文件绝不会中断整批**。

### 1.5 并发与线程模型

- GUI 主线程只负责绘制；所有耗时工作（IO / 解密 / 网络）在 `DecryptWorker(QThread)` 中执行。
- 流水线内部是**串行**处理文件（顺序、可预测、便于日志阅读）；每个文件内部对音源串行、对候选串行（天然限速，降低触发反爬的概率）。
- 工作线程通过 Qt 信号（`logged / progressed / file_done / completed / failed`）回传，跨线程自动走队列连接，无需加锁。
- “停止”按钮 → `worker.cancel()` → `Pipeline.cancel()` 设置取消标志，循环在检查点退出（不影响已写出的文件）。
- 回调防护：所有调用方回调（日志/进度）都经 `_guarded()` 包裹。**即使回调自身抛异常**（例如在 GBK 控制台打印 emoji 触发 `UnicodeEncodeError`），也只会被吞掉，**不会中断解密**。

---

## 2. 技术栈选型与理由

### 2.1 主选方案：Python + PySide6（Qt6）

| 关注点 | 说明 |
|---|---|
| 美观 | Qt6 原生控件 + QSS 深色主题，接近现代桌面观感，远好于 Tkinter 默认皮肤 |
| 稳定性 | 成熟的事件循环、`QThread`、拖拽（`dragEnterEvent/dropEvent`）、表格、进度条 |
| 打包 | PyInstaller 官方 hook 支持 PySide6，可裁剪到只带 `QtCore/QtGui/QtWidgets` |
| 依赖 | 仅 Qt 运行时，无浏览器/Node 依赖 |

**各组件职责**：`QApplication` 提供事件循环；`QMainWindow` 承载界面；`QTableWidget` 显示待处理/结果列表；`QPlainTextEdit` 作为日志区；`QThread` 承载后台解密；`QFileDialog` 负责选择文件/目录。

### 2.2 为什么不用备选方案

- **pywebview + Vue/React**：观感好，但要额外维护前端构建链、`webview2` 运行时依赖，单文件打包更脆弱，且工具本身界面很“表单化”，Qt 原生控件已足够，性价比更高。
- **纯 Tkinter / ttk**：零依赖、包体小，但默认观感朴素，做“美观、简洁”需要大量自绘，收益低。
- **PyQt5**：许可为 GPL（商业分发需付费），本项目为个人使用虽无碍，但 PySide6 为官方 LGPL 绑定，更稳妥。

### 2.3 零依赖密码学（关键取舍）

不自带 `pycryptodome`，而是**自实现纯 Python 密码学**，避免原生扩展与 PyInstaller 的二进制打包坑：

- `mmusic/crypto/aes.py`：AES-128/192/256 ECB 加/解密。S 盒**按 FIPS-197 算法即时生成**（而非手抄 256 字节表），杜绝抄写错误；已用 FIPS-197 三组标准向量验证。
- `mmusic/crypto/tea.py`：TEA 分组密码 + 腾讯 TEA-CBC 密钥包裹（`salt/zero` 变体）。
- 音频层的流密码（NCM 键盒、QMC 静态/映射）本质都是“与一段短密钥逐字节异或”，因此用**大整数异或**一次完成，把 O(n) 的 Python 循环换成几次 C 级调用，几 MB 文件毫秒级完成。

### 2.4 网络层：urllib 而非 requests

自研 `mmusic/net.py`（标准库 `urllib`）：更小的包体、无需携带 `certifi`、无 PyInstaller hidden-import 麻烦。内置：连接/读取超时、指数退避重试（对 408/429/5xx）、gzip 解压、流式下载（带进度与大小上限）、把 HTML 响应识别为“非音频”。仅对**瞬时**错误重试；4xx（除 429）直接返回状态码，交给上层**切换音源**。

### 2.5 元数据：mutagen（可选）

**配置目录解析（v1.0.21-dev1）**：`mmusic/config.py:resolve_config_dir()` —— 程序目录（冻结后 = exe 所在目录，
见 `mmusic/paths.py:program_dir()`，**不是** `_MEIPASS` 解包临时目录）下若存在 `settings.json` 或 `portable` 标记，
配置与哈希缓存就落在该目录（便携模式，随 exe 搬走）；否则回退 `%APPDATA%\MusicDecryptor`（与原行为一致）。
`.gitignore` 已屏蔽 `settings.json` / `portable`，**便携目录里的凭证绝不入库**。

`mmusic/metadata.py` 用 mutagen 写 ID3(v2.3/v2.4)、FLAC(Vorbis + Picture)、OggVorbis、MP4。**未安装 mutagen 也不影响解密主流程**，只是不写标签（未安装时标签与封面不会写入音频文件）；除此之外**不会产生任何额外文件**。

### 2.6 打包：PyInstaller

`--onefile` + 自定义 `.ico` + `console=False`。见第 5 节。

### 2.7 依赖清单

- 运行时：`PySide6>=6.10,<7`、`mutagen>=1.47`。
- 构建期：`pillow>=10`（生成图标）、`pyinstaller>=6.16`。
- 可选（生成文档）：`python-docx`。
- **不依赖**：`requests`、`pycryptodome`、`numpy`、任何付费 API。

---

## 3. 核心算法与关键代码

> 以下算法均移植自公开开源实现：`unlock-music` 的 `qmc_cipher.ts` / `qmc.ts` / `qmc_key.ts` / `ncm.ts`、
> `qmcdump` 的 `crypt.cpp`。`tests/fixtures.py` 实现了对应的**加密**侧，用于端到端自检。

### 3.1 格式识别（`sniff.py`）

```python
def sniff(head, tail, ext) -> FormatInfo:
    if head.startswith(b"CTENFDAM") or ext == "ncm":
        return FormatInfo("ncm", ext, "mp3")
    if ext in QMC_EXT_MAP:                       # qmc0/qmc3/mgg/mflac/...
        _family, out_ext = QMC_EXT_MAP[ext]
        if tail[-4:] == b"STag":                 # 新版：文件内无密钥
            return FormatInfo("qmc", ext, out_ext, needs_online=True, ...)
        return FormatInfo("qmc", ext, out_ext, trailing=tail[-4:])
    if (a := looks_like_audio(head)):            # 已是明文音频
        return FormatInfo("plain", ext or a, a)
    return FormatInfo("unknown", ext, ext)
```

`looks_like_audio` 依据魔数表判定：`fLaC / OggS / RIFF / MAC␠ / ID3 / 0xFFFB|F3|F2|FA / 0xFFF1 / FRM8`，以及偏移 4 处的 `ftyp`（m4a）。

### 3.2 NCM 解密（`crypto/ncm.py`）

容器布局（`CTENFDAM` 之后）：

```text
2   未知
4   密钥长度 u32LE      → 每字节 ^0x64 → AES-128-ECB(NCM_CORE_KEY) → 删 17 字节前缀 → 16 字节 RC4 密钥
4   元数据长度 u32LE    → 每字节 ^0x63 → "163 key(Don't modify):"+base64 → AES-128-ECB(NCM_META_KEY) → JSON
4   CRC32
5   未知
4   封面长度 u32LE
N   封面图
..  音频：byte i ^= key_box[i & 0xff]
```

两种密钥常量（均为公开值）：`NCM_CORE_KEY = 68 7a 48 52 41 6d 73 6f 35 6b 49 6e 62 61 78 57`，`NCM_META_KEY = 23 31 34 6c 6a 6b 5f 21 5c 5d 26 30 55 3c 27 28`。

关键点——**音频偏移自校验**。因为音频是“下标相关”的异或流，不能盲目搜索魔数；但可以**用候选偏移解密头部，看是否命中真实音频魔数**，命中即可信：

```python
def _audio_start(self, keybox) -> int:
    base = self.offset
    def looks(off):                      # 用键盒解出前 16 字节，看是否像音频
        head = bytes(self.raw[off+k] ^ keybox[k & 0xFF] for k in range(16))
        return looks_like_audio(head) is not None
    for off in (base+13+self._u32(base+5), base+13+self._u32(base+9)):   # 两种已知布局
        if looks(off): return off
    for off in range(base, min(base+96, len(self.raw)-16)):              # 兜底窗口扫描
        if looks(off): return off
    return base + 13 + self._u32(base+5)                                  # 最后退回文档布局
```

这让解码器对实现之间细微的偏移差异**免疫**，且完全不需要联网。

### 3.3 QMC v1 / v2 解密与密钥派生（`crypto/qmc.py`）

三种流密码：

- **QMCv1 静态**：`mask(i) = box[(i*i + 27) & 0xff]`（`i>0x7FFF` 先 `%= 0x7FFF`），`box` 为 256 字节静态表（见 `constants.py`）。
- **QMCv2 映射（key ≤ 300）**：`idx=(i*i+71214)%n`；`mask = rotl(key[idx], (idx&7)+4 后取模 8)`。
- **QMCv2 RC4（key > 300）**：分段（首段 0x80、其后每段 5120），按 `hash`/段号派生跳过长度，做段内 RC4-KSA 变体。

尾部三种形态决定走哪条路：

```python
tail = self.file[-4:]
if tail == b"STag":                 # 新版：无内嵌密钥
    raise NeedsOnlineError("QQ音乐新版加密（STag：文件内无密钥，需联网/客户端密钥）")
if tail == b"QTag":                 # 内嵌密钥 + 歌曲 id
    key_size  = u32be(file[-8:-4]); audio_size = size - key_size - 8
    raw_key   = file[audio_size : size-8]
    key       = qmc_derive_key(raw_key.split(b",")[0])
    cipher    = RC4(key) if len(key) > 300 else Map(key)
else:                               # 旧版：末 4 字节为密钥长度（小端）
    key_size = u32le(file[-4:])
    if key_size < 0x400:  key = qmc_derive_key(file[size-key_size-4 : size-4]); cipher = ...(key)
    else:                 cipher = Static()          # 无密钥 → QMCv1 静态
```

**musicex（2026 新版 `.mgg` / `.mflac` / `.mmp4`）**：音频后接**未加密尾部**（A 变体 192 字节；B 变体 200 字节 + 16 字节前缀），尾部以 `musicex\0` 结尾、用 UTF-16LE 存 `song_mid` 与原始文件名，**不含密钥**。ekey 需用账号在线获取（每文件、约 22 小时有效）：本地 `ekeys/<song_mid>__<filename>.txt`（社区 `fetch_ekey.py` 的默认布局）存在时直接展开解码；否则抛 `NeedsOnlineError` 转备用搜索。**自 v1.0.19 起**：设置页可填自己的 QQ 会话（`qqmusic_key`，或直接粘整条 Cookie）——处理到 musicex 文件且缺 ekey 时**按需静默拉取一次**（官方 `GetEVkey`：每曲一次、串行、瞬时失败 3 秒后重试一次、22 小时缓存），写入 `ekeys/` 后**当次即可离线解码**；未配置凭证或拉取失败则完全维持原行为。凭证只存本机 `settings.json`、日志永不输出。另加**解码后安全网**：结果必须通过音频头校验（`looks_like_audio`），否则一律判失败 —— 未知新格式、错误密钥、被误判的“静态加密”都不可能再静默产出垃圾文件。**自 v1.0.21-dev1 起**：本地 `ekey`
 只有落在 **22 小时**新鲜窗口内，`sniff_path` 才把它视作“可离线解码”；过期 `ekey` 一律保持 `needs_online=True`，交由流水线按需
 静默重取。若一份“新鲜”的 `ekey` 仍解不出音频（换机器 / 被提前轮换），只要已配置凭证就**静默强制重取一次**（`force=True`、
 8 秒短超时）再试，仍失败才如实上报并转备用链路 —— 陈旧 `ekey` 不再绕过重取而直接被判成“密钥不匹配”。

密钥派生（`qmc_derive_key`）核心三步；其中腾讯 TEA-CBC 的“双解密”结构按参考实现 1:1 移植：

```python
raw_dec = base64.b64decode(raw)
raw_dec = _decrypt_v2_key(raw_dec)                     # 处理 "QQMusic EncV2,Key:" 前缀
simple  = simple_make_key(106, 8)                      # int(abs(tan(106 + i*0.1))*100) & 0xff
tea_key = interleave(simple, raw_dec[:8])              # 取奇数位填入
sub     = decrypt_tencent_tea(raw_dec[8:], tea_key)    # TEA-CBC(salt/zero) 变体
return raw_dec[:8] + sub
```

`tests/fixtures.py` 里实现了其**逆运算** `encrypt_tencent_tea`，并验证 `decrypt(encrypt(x)) == x`，从而证明该移植与参考实现自洽。

### 3.4 MGG / MFLAC 与“需联网破解”判定

`.mgg* / .mflac* / .mmp4` 属于 QMCv2 家族，**能否离线取决于尾部**：

- 尾部 `QTag`（内嵌密钥）→ **可离线**解密。
- 尾部 `STag`（无内嵌密钥，密钥由客户端联网取）→ 判定 `needs_online=True`，抛 `NeedsOnlineError`，由编排层**立即转备用下载**。
- 旧版 `mflac/mgg` 若末 4 字节密钥长度 < 0x400 → 有内嵌密钥 → 离线解；否则视为静态 QMCv1。

### 3.4 目标音质与可选转码引擎（`mmusic/transcode.py`）

「目标音质」自 v1.0.18 起**真正决定最终产物格式**（解密输出与备用下载两条路径统一生效）：

| 源格式 | 目标 MP3 | 目标 FLAC | 目标 WAV |
| --- | --- | --- | --- |
| 有损（.mgg→OGG、M4A、WMA…） | → **MP3 320k** | **降级 MP3**（日志注明“未做无效升格”） | **降级 MP3** |
| 有损（已是 MP3） | 原样（零开销） | **保持原样**（不重编码；如实标注"不做无效转换"） | **保持原样** |
| 无损（FLAC / WAV / APE / DFF） | → MP3 320k | → FLAC（WAV/APE→FLAC） | **保持无损**（FLAC 不展开成 WAV） |

**标签与封面在转码中随行（v1.0.21-dev1）**：离线解密出的音频后，先**回读源文件自带的标签**（`mmusic/metadata.py:read_source_meta`，只解析头部、毫秒级、纯本地）——
① 只要源里有 `title/artist/album` 就用它，**不再把正确的标题覆盖成文件名**（此前 `.mgg` 解出的“夜曲”会被写成“周杰伦 - 夜曲”）；
② 源里若**内嵌封面**，转码后写入产物（MP3 → ID3 `APIC`，FLAC → `PICTURE`，M4A → `covr`），**不再因为转码而丢弃**。
**源里没有封面时绝不联网找图**（不增加任何网络请求与等待；`fetch_cover` 只作用于原有的“备用下载”路径）。
成本：每文件一次头部解析（实测 ≈0.5 ms/3MB，首次调用含 mutagen 冷启动 ≈35 ms，每进程仅一次），相对单曲 2~3 s 的转码可忽略。

**引擎（方案 1，2026-10-18 用户拍板）**：复用 **PySide6 自带的 FFmpeg 库**（`avcodec/avformat/avutil/swresample`，
LGPL；Qt 6.8+ 的多媒体后端本来就是 FFmpeg）——**不新增任何外部依赖、不打包 `ffmpeg.exe`**；打包时附带这 4 个 DLL
（exe 约 47MB → 约 55MB），运行时 ctypes 动态加载。**引擎缺失 / 加载失败 / 转码失败一律静默回退：保留原格式并写一行日志**，
绝不丢文件——这就是“无引擎版本”的回退语义。被否决的替代方案：打包现成 `ffmpeg.exe`（体积 +50~90MB、多为 GPL 构建、
杀软误报高）、依赖系统 ffmpeg（用户需自行安装，否则目标音质形同虚设）。

实现细节（版本无关的自发现，Qt 这份是 FFmpeg 8 系；**音频流定位必须校验**：堆内存里
`codec_type == 1` 的位置不止一处 —— 实测裸 FLAC 曾命中无关内存得到 `codec_id=0`，
误报“没有可用的解码器”并把转码静默降级，且**换文件顺序就又好了**。因此
`_find_audio_stream` 用 `avcodec_find_decoder(codec_id)` 逐个候选校验，只接受
“确实存在解码器”的那一个）：ctx 字段偏移用 **AVOption 表枚举**（`ar`/`ch_layout`/`frame_size`；
`sample_fmt` 位于 `ar` 与 `frame_size` 之间、默认 -1 故可定位）；AVFrame 的 `ch_layout` 偏移用**写入探测**（写入合法布局后以
`av_frame_get_buffer` 成功为证，只碰零值槽位）；所有指针访问走 `ReadProcessMemory` 安全探测。编码器用 Windows 内核
`mp3_mf`（无 libmp3lame/GPL 负担）与 libavcodec 的 `flac`；容器（裸 MP3 帧流 / `fLaC`+STREAMINFO+帧）由我们自己写，
不用 libavformat 输出侧。不重采样（保持源采样率/声道）；MP3 编码仅支持 32/44.1/48kHz、1~2 声道，其余如实放弃。

### 3.5 备用搜索引擎接口（`search/base.py`）

统一抽象，新增音源只需实现 `search()`（必要时覆写 `resolve()`）：

```python
class SearchSource(ABC):
    name: str; label: str; offline = False
    def __init__(self, http, settings): ...
    @abstractmethod
    def search(self, query, *, quality="mp3", limit=10) -> list[Track]: ...
    def resolve(self, track, *, quality="mp3") -> Track:      # 填 track.url / ext / bitrate
        return track
```

已落地并**实测可用**的适配器：

- **Kuwo（酷我）**：`search.kuwo.cn/r.s`（伪 JSON，用“括号深度扫描器”解析含嵌套对象的条目）+ `https://antiserver.kuwo.cn/anti.s?type=convert_url3` 直链（2026-10-19 起走 HTTPS；实测 http/https 返回完全一致，明文无收益只有风险）。**直链形态即"正片/短资源"的指纹**：`/resource/30106/trackmedia/…` = 正片（实测 3.69MB/230s、3.00MB/188s，与声明时长一致，与 gequbao 直链同构）；`/ev/ /lx/ /gf/ /nf/` + `/resource/n2|n3/…` = 短资源（实测 0.08MB=5s、0.13MB=22s、0.18MB），由 `classify_link()` 零网络开销判定并置 `Track.short_asset`。注意**声明时长也不总是对的**（实测 `MUSIC_345182154` 声明 214s、直链实测 0:05），因此它只作排序参考，硬判定一律用实测时长。
- **Kugou（酷狗）**：`songsearch.kugou.com/song_search_v2`（标准 JSON）+ `m.kugou.com/app/i/getSongInfo.php` 直链（免费曲目返回 `url`，付费曲目 `url` 为空 → 自动跳过）。2026-10-19 实测（8 首歌 × 前 3 候选、56 次请求、6 次命中）：命中来源 **File 4 / HQ 1 / SQ 1**（故 `quality=mp3` 时把 `FileHash` 提到第一位）；**不论哪档 hash 返回的都是 128k mp3**，所以 `bitRate`/`extName` 才是事实、声明字段只作线索；是否放行按曲目逐个判定（《卡农》《稻香》给、《夜曲》不给）→ 它是**冷门/免费曲的补充源**，不是热门曲主力源。实测 `privilege_filter=1`（只搜免费曲）与 `=0` 结果相同，**未采用**。
- **Migu（咪咕）**：`search_all.do`（标准 JSON，`searchSwitch` 必填）+ `content/listen-url`（`channel` 走**请求头**；免登录 PQ 128k，HQ/SQ 遇登录墙自动降级）。实测直链字节数与官方声明一致、时长探测与完整度门禁均通过。
- **Aggregator（MyFreeMP3 / 聚合网关）**：可配置的 `api.php` 风格接口（默认 `music-api.gdstudio.xyz`），`types=search|url|pic|lyric`，实测可返回 NetEase 320kbps 直链与封面。**地址/平台可在设置里改**。
- **Local（本地曲库 / CC0 音效库）**：扫描用户指定目录，`file://` 命中即用，完全离线、优先级最高。

> 设计上把“平台是否长期可用”隔离在单个适配器内：某个站点改版/反爬，只需替换该文件，不影响解密核心与其它音源。

### 3.5.1 置信度成本：探测预算与"疑似短资源"（`pipeline._probe_durations`）

时长核验每次只探测 `MAX_PROBES`(2) / `MAX_PROBES_WIDE`(4) 个候选 —— 预算是稀缺资源。
2026-10-19 发现并修掉一个真实缺陷：**两个 5~22 秒的"短资源"就能把名额占光**，让真正
完整的候选根本没被测到，于是明明有原曲却报"未找到原曲"（A/B 实证：旧逻辑
`status=failed`「已排除 2 个时长偏短的候选」；新逻辑下到 40048B 真原曲）。

* `Track.short_asset`（直链形态判定）为真的候选**不占探测预算**：先探测疑似正片，
  预算有余才回头补测它们（`MAX_EXTRA_RESOLVES=2` 为"多看几个"封顶，成本有上界）；
* 通过时长门禁的候选里，`short_asset` 只作**同分次序**（`sort(key=(-score, short_asset))`），
  匹配度仍是第一判据，**绝不硬性排除**；日志用「已降权 N 个疑似短资源」如实说明；
* 探测结果回写 `Track.bitrate`：用 **体积 ÷ 实测时长** 算平均码率（不用首帧码率，
  VBR 首帧可能只有 32k 会造成误判），修正平台声明（酷我"声明 320 → 实际 128"甚至 48k）。

### 3.5.2 按目标音质的静默重排与偏长防护（v1.0.22）

* **目标无损（FLAC/WAV）→ 聚合通道优先**（`_LOSSLESS_FIRST_SOURCE`）：实测（2026-10-20）
  它是全链**唯一**能真给无损的源（`br=999` → 23.57MB / 806kbps FLAC）；酷我/酷狗/咪咕在
  flac 目标下都只给 128k mp3。**两处同时生效**：① 检索顺序把它提到联网源最前（占满探测预算）；
  ② 候选排序在 `verified` 之间把它排到最前（这才是决定"最终下载哪一个"的那一步 —— 只改检索顺序
  是表面功夫）。**诚实边界**：聚合通道走第三方曲库，热门版权曲常给不出过门槛的干净候选
  （实测《夜曲》被歌手/版本门槛全数刷掉），所以这条优先只对它能命中干净候选的曲目有效。
* **偏长防护**（`_over_tolerance` = `max(180s?, …)` 见代码：`max(TOL_OVER_ABS=120s, 0.6×参考)`）：
  实测时长**超过参考 + 容差**即视为合辑/串烧，排除并记日志（探测期一次、下载后一次）。
  没有参考时长时**不启用**，避免误杀本来就长的曲目（古典/现场）。触发样本：IA 的 25:29 / 435MB。
* **源已是有损 MP3 + 无损目标**：`plan()` 返回 `keep`（不重编码）—— 旧行为会把 128k 转成
  320k（实测 3.63MB → 9.07MB），体积翻倍却零收益、还多一代损失；这正是用户要求过的
  "自身是有损源，不进行无效转换"。

**「末位来源」政策（v1.0.21-dev1，`_LAST_RESORT_SOURCES`）**：2026-10-19 三十首混合曲目
批量实测（333 次请求）给出判决依据：

| 音源 | 可用候选（≥45s） | 平均请求/首 | 单独可用（只有它能拿到） |
| --- | --- | --- | --- |
| 咪咕 | **30/30** | 2.0 | — |
| 酷我 | 21/30（另 8 条是 5~22s 短资源） | 2.0 | — |
| 酷狗 | 16/30 | **7.1** | **0 首** |

酷狗命中里还大量是准正版（`夜曲 - A35`、`晴天 (Live)`、`孤勇者 (DJ版)`、`天空の城…(网友改编)`）。
因此：**照常检索**（保留"万一只有它有"的兜底能力），但 (a) **不为它预解析直链**、
(b) 探测/下载排到最后、(c) 只有其它来源**一个达到时长下限的候选都没测到**时才用
独立额度（`MAX_EXTRA_RESOLVES=2`）回头补测它。实测同一首歌（《晴天》）端到端请求数
**12 → 9**（酷狗 4 → 1）、耗时 10.3s → 3.5s，**产物完全一致**（零召回损失）。

### 3.5.3 健壮性矩阵与"绝不静默产出垃圾"（v1.0.22）

`tests/test_robustness.py`（可 `--quick/--soak/--seed`）覆盖五类：

| 类别 | 覆盖 | 判据 |
| --- | --- | --- |
| 边界 | 0 字节 / 1 字节 / 只有魔数 / musicex `tail_size` = 0、2³¹、2³²−1、=文件长度、4097 / FLAC 采样率 0 / 16KB 随机数据 / 保留名 `CON` / 300 字符名 / 坏类型 settings | 不崩、给出可读值 |
| 负面 | 5 种容器 × 4 种破坏（截断/翻位/尾垃圾/清零）、乱码 ekey、坏 settings/hashcache/台账/源清单、坏补链 | **要么可读失败、要么成功且产物通过音频头校验** |
| Fuzz | 容器变异（`--soak` 下每类 3000 次）+ 纯解析器随机字节（2 万次）+ 随机文件名 | 无未预期异常、无静默垃圾；解析器**永不抛异常** |
| 故障注入 | 检索/解析/下载抛错、返回网页/过小/垃圾、本地"坏服务器"（挂死、截断、4GB 声明、500、伪造 gzip）、输出目录被文件占位、源文件消失、传入目录、取消 | 有界失败 + 可读原因 + 不产出文件 |
| 内存 | 300 次重复处理、客户端/台账 200 次 churn、2 万行日志 | 工作集增长有界（实测 +7~10MB）、无持续泄漏趋势、日志控件仍限 4000 行 |

**"绝不静默产出垃圾"现在是三个解码器的一致契约**：QMC 早已有，本版把 **NCM** 与 **KWM** 补齐 ——
解码结果过不了 `looks_like_audio` 就抛 `DecryptionFailedError`（而不是"按声明格式硬给个扩展名"
或"只写条警告"）。深度 fuzz（4 个种子 × 1.5 万次变异）正是这样挖出 NCM 的 `struct.error` 逃逸与
KWM 的静默垃圾的。

**原子写**：所有产物先写同目录 `.{name}.part`、再 `os.replace` 到位 —— 断点/崩溃不会留下半个文件
（否则下次只会另存成 `(1)`，用户拿到一个"看起来正常"的残缺歌）。

**句柄纪律**：`ResourceIndex._connect()` 的异常分支必须 `close()`（本版修复的泄漏）——
凡"打开-使用-关闭"的资源，异常路径与正常路径都要关闭，这类泄漏靠 fuzz 是抓不到的，只能靠
"循环 churn + 工作集测量"这种测试盯。

### 3.5.4 UI 猴子测试与 QThread 生命周期（v1.0.22）

`tests/test_monkey_ui.py`（`--seed/--iters/--rounds`）把界面上**几十个控件**随机组合点一遍：
按钮/勾选/下拉/输入框（含 300 字符、emoji、`CON`、非法路径等脏值）/表格选择/清空/`添加文件`
（文件对话框被替换成非阻塞桩）；**拖拽投递**（`QDropEvent`/`QDragEnterEvent`，含"根本不是文件"
的 mime）；设置与补链对话框的**全控件乱点**（并把 `apply_to` 的结果做"合法性"断言）；
任务的乱启停（起→停→起、跑到一半改设置/清空列表）；以及**关窗时线程还在跑**。
模态入口（`QDialog.exec`、`QFileDialog.*`、`QMessageBox.*`、`QDesktopServices.openUrl`）在测试里
全部换成非阻塞桩，`QSettings` 用独立 org/app 名隔离（不污染用户窗口几何）。

**它挖出的最重要缺陷：QThread 生命周期**。规则（以后写异步代码必须遵守）：

* **绝不能在"发出信号的那个槽"里丢掉 QThread 的最后引用** —— `completed`/`failed` 是在
  `run()` 内部发出的，此刻线程尚未返回；`self._worker = None` 会让 Python GC 先于线程结束
  销毁 C++ 对象。正确做法：把 worker 移入"待回收"列表，等 `QThread.finished` 到了再 `deleteLater()`。
* **关窗/退出必须等线程收尾**：`closeEvent` 里 `cancel()` 之后要有**有界等待**（本项目 5 秒），
  否则窗口/进程先死、线程后死 → Windows 直接给 `0xC0000409`（STATUS_STACK_BUFFER_OVERRUN，
  不是 Python 异常，`faulthandler` 也抓不到）。
* 这段教训固化为冻结产物门禁：`MusicDecryptor.exe --check-worker`（旧代码必崩、新代码退出码 0，
  A/B 已验证）。

### 3.10 以名找歌：与备用链路共用同一个选源核心（本轮新增）

**动机**：日常"点播取歌"里，很多歌在网易云/QQ 并没有下载入口（或没有 VIP 下载文件），
用户只能去第三方站手动搜 + 等倒计时。既然备用链路本身已经是"按名搜索 → 硬门槛 → 时长核验 →
下载校验 → 落盘"，缺的只是一个**不依赖输入文件**的入口。

**实现（关键：不复制逻辑）**：把原来的 `_fallback` 拆成三块，两条入口共用：

| 层 | 函数 | 职责 |
| --- | --- | --- |
| 解析 | `_seed_from_name` / `_seed_for` | 名字 → `(读法候选, 查询串, 点名版本标记)`（与文件名模式同构） |
| 检索 | `_collect_pool` | 查询扩展 + 逐源 `_search_and_gate` + 华语/无损排序 → 干净候选池（**不下载、不探测**） |
| 收尾 | `_take_from_pool` | 跨平台参考时长 → 探测 → 短/长/过短过滤 → 下载 → 校验/完整度 → 落盘 |

* 文件名模式：`process_file` → `_fallback`（seed = `src.stem`，落盘后删源文件）；
* 以名找歌：`acquire` / `acquire_many`（seed = 用户填的 `歌手 - 歌名`，`src=None`，**不删任何文件**）；
* 人工挑选：`search_candidates` 只跑 `_collect_pool`，返回**已过硬门槛**的候选。

**只给歌名时的"静默共识"（用户 2026-10-21 拍板选 2）**：`readings` 里一个歌手都没有时，没有任何
歌手参照可比对，实测会出现"命中一首同名翻唱"（`--query 夜曲` → 取到别人的版本）。因此增加：
统计通过门槛的候选里各演唱者出现次数，把**出现最多的那位**排到前面（同一位歌手的多种写法
按**包含关系**归并，避免被平台命名差异拆散）。

* **只排序、不排除**：候选一个都不删（多数不一致时等价于"分数最高的仍在前面"，不会凭空制造偏好）；
* 参与两处排序：候选池排序（决定探测名额给谁）与"最终下载谁"的排序 —— 两处都必须带，
  否则前 2 个探测名额会被别的演唱者吃掉，真正该测的那个根本没被测到；
* **静默 + 如实**：只写一行 detail 级日志（`只给了歌名：按候选里最多的演唱者「周杰伦」优先（3/11 …）`），
  不需要用户额外输入，也不新增任何门槛或开关（对齐 `AGENTS.md` §4）；
* 仍是**偏好不是保证**：最终下载谁照样要过时长核验与完整度门禁。

**输入清洗：序号与符号（`match.sanitize_query_text`，本轮新增）**：点播清单/聊天里粘来的一行
常常带列表序号（`1.` `2、` `三、` `（一）` `【3】` `第 3 首` `01␣`）或装饰符号（`•` `★`、`《》` 包裹、
零宽字符）。它们同时污染两处 → **发给音源的检索词**（`1. 夜曲` 搜不到东西）与
**歌名相似度硬门槛**（实测 `1. 夜曲` vs `夜曲` 只有 0.57 < 0.60，直接"匹配不到"）。

* 清洗顺序：NFKC + 去零宽 → 整串（含书名号/引号包裹、前后缀、叠加形态如 `★ 2.（一）` 迭代 4 轮）
  → **再按分隔符逐段清洗**（`周杰伦 - 1. 夜曲` 这种"序号在歌手之后"的形态靠这一步）；
* 逐段用捕获组切开并**原样保留分隔符**，所以 `Love-Story` 这类连字符标题不会被插入空格；
* **精度护栏**（宁可不洗，也不洗错）：括号内容必须是**纯数字/中文数字**才算序号（`（Live）`
  不会被当序号）；裸数字必须**后接分隔符**（`.．、,，)）:：-–—`）或写成**零填充**（`01`）才去掉 ——
  于是 `7 Years`、`30 岁的女人`、`2002年的第一场雪` 都保持原样；清洗结果为空时返回原文。
* **只增不减**：清洗后的读法之外，**清洗前的原读法也保留**（`_seed_from_name`），
  所以最坏情况与清洗前一致，不会因为清洗反而失配。
* **空格读法**（`match.space_split_readings`）：手动搜索框里 `夜曲 周杰伦` 这种写法很自然，
  但空格不是文件名里的分隔符约定 → **只补读法**（`(夜曲, 周杰伦)` / 多词时取首段与尾段两种切法），
  不替换原有读法，排名仍取各读法最大值。

**占位歌手不当证据（2026-10-21 实测修复）**：`artist_match_score` 对"候选没有歌手信息
（空 / 未知 / **群星**）"刻意返回 1.0（无法判定就不淘汰）。于是出现了一条真实坏路径：

```
查询「周杰伦 - 夜曲」
  → 聚合源返回「夜曲」(群星) 4:36，歌手门槛按"无法判定"放过
  → 参考时长"取最完整值"取到 4:36 → migu 正确的 3:47 被当成"偏短"排除
  → 最终下载了「群星 - 夜曲.flac」   ✗ 用户明确点了歌手，却拿到别人的版本
```

修法（**只降权、不硬删**，与"短资源/末位来源"同一套哲学）：

* **查询里给了歌手**时，占位歌手候选（`群星`/`未知`/`VA`/`Various Artists`… 或含 `群星`/`合辑`/`未知`）
  不参与参考时长的 `declared` / `measured` —— 它的长度可能是别人的录音，不该当"最完整值"；
* 最终下载排序里排在有明确歌手的候选之后；
* **空串不算占位**（CC/PD、本地曲库常常没有歌手信息 —— 那是"没有信息"，不是"写着别人"）；
* 一个明确歌手的候选都没有时**原样使用占位候选**（照常下载，绝不因为这条规则变成"没找到"）。

**最后一轮输入清洗（v1.0.23）**：为了"让匹配度最大化"，把**输入噪声**按风险从低到高逐类收干，
每一类都先跑"输入 × 候选"失败矩阵再动手（不凭猜改）：

| 噪声形态 | 例子 | 处理 |
| --- | --- | --- |
| 列表编号（数字/中文数字/`#`/`No.`/`第N首`/零填充） | `1.` `三、` `#1` `No.3` `第 3 首` `01␣` | 前缀清掉 |
| 说明标签（白名单，共 29 个词） | `点播：` `歌名：` `推荐 -` `【点播】` `点播 ` `求：` | 前缀清掉；**整段就是标签**的直接丢 |
| 分隔符书写差异 | `周杰伦《夜曲》` `周杰伦·夜曲` | 书名号/间隔号当分隔符 → `周杰伦 - 夜曲` |
| 粘贴文件名 | `夜曲.mp3` `周杰伦 - 夜曲.mflac` | 尾部扩展名清掉 |
| 歌手提示 | `夜曲 原唱：周杰伦` `夜曲（原唱：周杰伦）` | **抠出歌手**补成读法（只认括号内/行尾两种无歧义形式） |
| 只有噪声的行 | `1.` `求：` `点播` | `looks_like_song_query` 判定不可用 → 批量时**跳过并在结果里标注** |

精度护栏（每条都有用例）：版本标记（`(Live)`/`(伴奏)`/`男声`）原样保留；`7 Years`/`30 岁的女人`/
`2002年的第一场雪` 不动；`音乐会` 不被当标签；`Love-Story` 不被插空格；清洗结果为空时回退原文；
清洗前的原读法始终保留（只增不减）。**歧义不猜**：`歌手：周杰伦 夜曲` 不做推断。

**门槛不放松（红线）**：人工挑选只在"已经过歌名/歌手硬门槛 + 伴奏/改编过滤 + 时长核验"的集合里选；
用户点名的那条若没通过核验，就直接如实报错 —— 绝不退化成"用户点了就一定给"。

**身份匹配**：候选列表与下载是**两次独立检索**（对象不是同一个），因此挑选结果靠
`(source, dedupe_key)` 重新定位，而不是对象身份。

**输出目录**：`_out_dir_for(None)` → `default_search_dir()`（`%USERPROFILE%\Music\MusicDecryptor`）。
刻意**不落到进程工作目录**（双击 exe 时就是它自己的目录），这是历史事故留下的红线。

**成本**：不新增依赖、不新增线程模型（复用 `ui/worker.py` 的 `NameSearchWorker`，与
`DecryptWorker` 共享同一套 QThread 生命周期纪律，见 §3.5.4）。

### 3.6 下载与校验（`downloader.py`）

“下载前校验文件头与码率”的落地：

```python
def validate(data, expected_ext, *, expected_bitrate=0, duration=0, min_bitrate=0, min_bytes=16*1024):
    if len(data) < min_bytes:                     return FAIL("文件过小")
    if data[:1] in (b"<", b"{") or html_like:     return FAIL("返回的是网页/JSON，不是音频")
    ext = looks_like_audio(data[:16])
    if not ext:                                   return FAIL("文件头不是已知的音频格式")
    bitrate = mp3_info(data)["bitrate"]  if ext == "mp3"  else \
              flac_info(data)["bitrate"] if ext == "flac" else 0
    if not bitrate and duration:                  bitrate = len(data)*8/duration/1000
    if min_bitrate and bitrate and bitrate < min_bitrate: return FAIL("码率过低")
    return OK(ext, bitrate)      # 以“实际文件头”为准，而非音源声称的扩展名
```

`mp3_info` 解析 MPEG 帧头（跳过 ID3v2）得到码率/采样率；`flac_info` 解析 `STREAMINFO` 得到采样率/位深/总样本数以推算码率。由此可拦截：错误页、防盗链占位、扩展名造假的低质文件。

### 3.7 最佳匹配打分与文件名双读法（`match.py`）

```python
score = 0.72*similarity(q_title, t.title) + 0.28*similarity(q_artist, t.artist)
if duration: score += 0.12 * max(0, 1 - |Δduration|/15)      # 15 秒内视为同一版本
if prefer_ext and t.ext == prefer_ext: score += 0.06
```

**文件名双读法**：`歌手 - 歌名`（音乐 App 的命名习惯）与 `歌名 - 歌手`（用户手改时的常见形态）在**只给一个文件名时无法区分**。程序不猜，而是同时给出两种读法，用同一批搜索结果分别打分，取该曲在两种读法下的**较高分**：

```python
readings = parse_song_filename_candidates("晴天 - 周杰伦")
# -> [("周杰伦","晴天"), ("晴天","周杰伦")]
query = filename_query("晴天 - 周杰伦")             # "晴天 周杰伦"（保持文件里的词序）
rank_tracks_multi(readings, tracks, prefer_ext=quality)   # 每首取两种读法的最大值
```

- 关键词查询**只发一次**（各音源按“关键词袋”处理），不会因此增加请求量。
- 日志会打印实际采用了哪条读法，便于核对：`采用读法：歌名「晴天」/ 歌手「周杰伦」`。
- 两种读法同分时说明信息不足，此时按音源返回的原顺序取。

`similarity` = `difflib` 比值 与 词元重叠率 的融合；`clean_text` 先做全半角归一化、去括号噪声（`[mqms2]`、`(Live)`、`(Remaster)`…）。低于阈值（0.34）的候选**不进入下载**，避免“下载到一首同名但八竿子打不着的歌”。

> 该弱点的修复见 §3.8：括号内容虽不参与**相似度**计算，但会参与**版本过滤**。

### 3.8 版本过滤：只认原曲（`match.py`）

**问题**：`clean_text` 为了做相似度会把括号内容丢掉，于是 `晴天 (KTV版伴奏)` 与 `晴天` **同分**。实测搜索“晴天 周杰伦”时，酷我返回的首位候选正是 **KTV 伴奏版**——下载下来不是原曲。此外酷我还会返回 **演唱会串烧**（0.34 分），而旧逻辑是“第一个能用的音源就下”，它会抢在酷狗那条 1.02 分的正版之前被选中。

**规则**（用户要求：歌名字段强匹配，命中即排除；位置无关、大小写无关、忽略标点与变体）：

- 检查**原始字符串**（不剥括号）；字段覆盖歌名 + 各音源原始结果里的 `name` / `title` / `SongName` / `FileName` / `file` / `tags` 等命名类字段。
- **中文/日文标记按子串匹配**（不会出现在别的常用词内部）：
  `伴奏`、`伴唱`、`纯音乐`、`无人声`、`卡拉ok`、`カラオケ`、`翻唱`、`现场`、`试听`、`不插电`、`剪辑`、`演唱会`、`巡演`、`串烧`、`联唱`、`演出版`、`off vocal`、`minus one`
- **拉丁标记按词元匹配**（避免误伤）：
  `instrumental`、`inst`、`karaoke`、`remix`、`cover`、`live`、`demo`、`acoustic`、`edit`、`mix`、`concert`、`medley`
  因为是词元匹配，`Alive` / `Instant Crush` / `Mixed Up` / `Deliver` / `Oliver` / `Believer` **都不会被误伤**（有专门用例锁定）。
- **原曲偏好**：命中 `studio` / `original` / `official` / `原版` / `录音室` / `正式版` 加 0.03 分；标题完全没有任何括号修饰再加 0.02 分。

**例外（用户主动要伴奏）**：若**输入文件名本身**含上述标记（如 `晴天 (伴奏) - 周杰伦.mgg`），视为用户点名要该版本 → **不过滤**，但日志给出警告。

**跨音源择优**：过滤之后不再“第一个能用的音源就下”，而是把各音源的干净候选汇入统一候选池按分排序，**分高者先下、失败依次顺延**（上限 6 次）；若某音源已给出 ≥0.95 的高置信度原曲，则跳过其余音源以省请求。

**华语歌预匹配（静默、自动）**：备用链路的信源顺序随歌曲语种自动调整 —— 上传文件名/元数据含汉字，或歌手命中内置的常见华语歌手名单（含英文/拼音写法）时，本次按「**咪咕优先** → 其余主流源 → 维基共享 / 互联网档案馆垫底（且仅在主流源全无候选时才检索）」执行；非华语歌维持用户配置的原顺序。全程无需用户操作，只在日志里说明。

**无原曲时的提示**：所有音源的候选都被过滤掉时，报
`未找到原曲，当前仅有伴奏/改编版，请确认是否需要下载。`

**验证**：63 项自检中有 14 项专测本条：11 项版本过滤（24 个必中标记 + 11 个必经的正常标题 + 字段级检查 + 端到端“曲库里同时有伴奏与原曲时选原曲” + “文件名点名要伴奏则保留并警告”）+ 3 项跨音源择优（列在前面的弱候选必须输给后面 1.02 分的正版）。

### 3.9 时长交叉验证：用音频本身抓“名实不符”（`duration.py`）

**问题**：平台上有大量“歌名、歌手都正确，实际音频却是别的版本”的资源。标题过滤看不见这种问题，但**时长**能看见——剪辑版/劣质 remix 的长度几乎总是明显偏离原曲。

**怎么拿到“实际时长”（不下载整首）**：对候选直链发一次 **Range 请求只取 256 KB**（服务端不支持 Range 就读满 256 KB 即断开），再用纯标准库解析容器头：

- **FLAC**：`STREAMINFO` 的 `total_samples / sample_rate` → **精确**时长（与文件大小无关）。
- **MP3**：跳过 ID3v2 后解析 MPEG 帧头；带 `Xing`/`Info`/`VBRI` 标签则 `帧数 × 每帧采样数 / 采样率` **精确**；否则按 `文件大小 × 8 / 码率` 估算。
- **WAV**：`fmt ` 的字节率 + `data` 块大小 → 精确。
- **M4A**：`moov/mvhd` 的 `timescale` 与 `duration`（`moov` 不在文件头时取不到）。
- **OGG**：时长在最后一页，头部探测不到 → 判为“无法验证”。
- 每次只探测 `MAX_PROBES = 2` 个候选，探测超时单独收紧为 `PROBE_TIMEOUT = 8s`（慢就跳过，绝不拖慢主流程）；探测得到的直链会被缓存复用，下载时不再二次解析。

**防误判**：单个 `0xFF 0xEx` 同步字不足为凭——4 KB 随机数据撞上的概率约 **86%**。因此 MP3 帧只有在**带 Xing/Info/VBRI 标签**，或**紧随其后存在同版本/同层/同采样率的第二帧**时才被接受。

**参考时长怎么定（关键）**：由软件**静默、跨平台**取得，绝不只信“正在提供文件的这个平台”——实测同一首歌在不同平台的标注可能一个是截短的（周杰伦《最长的电影》：酷我 2:22 / 2:11，酷狗 3:55，只有后者完整）。

- 收集**所有音源**对同一最佳匹配的**平台标注时长**（搜索响应里免费带回来的），取“**最完整且不离群**”的值：`_plausible_max()` = 忽略超过中位数 2 倍的值之后取最大值。
  取最大值是刻意的——同一首歌长短不一时，短的那个几乎总是被截短/剪辑的版本。
- 再与**实测时长**合并取最完整值；容差 `max(TOL_ABS = 15s, TOL_REL = 10%)`。
- 因此**取消了“拿到直链就提前收工”**：那会让我们只看到一个平台的标注，恰好挡住了这条跨平台比对。

**淘汰与兜底（只下载通过验证的资源）**：

1. **标注预筛（免费，省请求）**：平台标注就明显偏短的候选直接排除，连文件都不用取。
2. **实测淘汰**：实测时长短于 `参考 − 容差` → 排除；实测 `< MIN_TRACK_SECONDS = 45s` → 视为前奏/试听片段排除。
   只判“偏短”即可：参考本身取的就是最完整值，不会有候选比它更长。
3. **只下载通过验证的**：一旦拿到参考时长，测不出或对不上的候选一律不作为可信选项——**宁可如实报“未找到原曲”，也不用没验证过的凑数**。
   只有连参考时长都取不到时，才允许兜底，并在结果里标注 `完成（时长未比对）`。
4. 只有一个候选、无法交叉比对 → 结果里陈述式标注 `完成（单一候选未比对）`。

**顺带修掉的两个相关缺陷**：

- **歌名字段硬门槛**（`title_match_score ≥ 0.60`）：以前“同歌手、不同歌”的结果只靠歌手分就能拿到 0.36 混进候选（搜“晴天”却收下“花海”“听妈妈的话”）。
- **提前收工条件收紧**：只有“最高分候选确实能取到直链”时才跳过其余音源；否则继续查——避免某源无版权时明明后面有可用原曲却空手而归。

### 3.9.1 静默与成本原则（用户要求，见 `AGENTS.md` §4）

用户明确要求：**置信度操作必须静默完成，不得要求用户提供参考时长**，软件保持**简单易上手、快速稳定**。因此：

- 版本过滤、歌名硬匹配、跨音源择优、时长核验**全部自动完成**，只在日志里说明做了什么；
  **没有任何弹窗、没有"请确认版本"、没有"请提供参考时长"、也没有新增需要用户配置的开关或阈值**。
- 结果提示一律**陈述式**（`完成` / `完成（时长未比对）` / `完成（时长存疑）`），不写成要求用户行动的问句。
- 成本封顶：探测候选 ≤ 2 个、探测超时 8s、失败即跳过；已解析的直链复用，不重复请求。
- 置信度无法完成时**照常产出结果并如实标注**，不阻塞、不追问。
- 实测单文件耗时（含搜索 / 过滤 / 择优 / 时长探测，媒体读取用本地文件替代）：
  **本地音源 0.02s；三个在线音源 1.0~1.6s**。

**诚实的局限（已知）**：

- **完整长度的翻唱/改编版仍可能通过**：它与参考时长一致，只能靠标题/艺术家里的 `cover`/`翻唱` 字样识别，而这些字样并不总出现在标题里。
- **OGG / M4A** 在只有文件头时取不到时长 → 归入“不可信”。由于现在“只下载通过验证的资源”，这类候选在有参考时长时**不会被下载**（宁可如实报“未找到原曲”）。
- **播放器兼容性（2026-10 实测）**：解密产出的 `.ogg` 是标准 Ogg Vorbis（19.42/22.61 共 20 个真实样本：逐页 CRC、三级头包、EOS 全部校验通过，且与旧产物逐字节一致），但 **Windows 10 自带的“音乐/Groove”与 Windows Media Player 不内置 Ogg Vorbis 解码**，打开会提示“无法读取”；Windows 11 新版“媒体播放器”与 PotPlayer 播放正常。`.mflac → .flac` 不受影响（Win10 原生支持 FLAC）。
- 若某平台的候选**全都被同样方式篡改**、且其它平台也没有该曲，跨平台比对就无从谈起——此时如实报“未找到原曲”。
- 以上都是**已知边界**，软件不会为此弹窗打扰用户。

---

## 4. 图形界面布局（线框 + 说明）

**面向“同一台机器、多人使用”的简化（v1.0.20）**：`QQ 音乐凭证` 收进**可折叠的“管理员一次配置”分组**（支持「从剪贴板粘贴 Cookie」一键导入整条 Cookie），日常使用者完全不必打开设置；主界面新增「打开输出目录」；`设置 → 行为 → 拖入文件后自动开始处理` **默认开启** —— 同事的完整流程 = **双击程序 → 把歌曲拖进窗口 → 完成**（输出按目标音质落在输出目录，表格逐条显示结果）。

### 4.1 主窗口

```text
┌───────────────────────────────────────────────────────────────────────────┐
│  音乐解密工具                                                             │
│  本地优先 · 离线解密 NCM / QMC / MGG，失败自动切换备用音源搜索下载        │
├───────────────────────────────────────────────────────────────────────────┤
│ [添加文件] [添加文件夹] [移除所选] [清空列表]       目标音质 [MP3（默认）▾]│
│ 输出目录 [______________________________________] [选择…] [打开输出目录]  │
│                          [以名找歌…] [手动补链…] [设置…]                 │
├───────────────────────────────────────────────────────────────────────────┤
│ 文件名                │ 状态     │ 格式 │ 输出文件            │ 说明       │
│ 稻香.ncm              │ 已解密   │ MP3  │ 周杰伦 - 稻香.mp3   │ 完成       │
│ 晴天.mflac            │ 备用下载 │ FLAC │ 周杰伦 - 晴天.flac  │ 来源 kugou │
│ 已处理过.qmc0         │ 已跳过   │ MP3  │ ...                 │ 哈希命中   │
│ 损坏文件.xyz          │ 失败     │      │                     │ 无法识别   │
├───────────────────────────────────────────────────────────────────────────┤
│ [██████████████████░░░░░░░░░░]   进度 3/4                                 │
├───────────────────────────────────────────────────────────────────────────┤
│ 运行日志   [ ] 显示详细信息                        已折叠 34 行详细过程   │
│ ✅ 解密完成：稻香.ncm → 周杰伦 - 稻香.mp3  [MP3]                          │
│ 🔒 需联网破解：晴天.mflac — STag：文件内无密钥，需联网/客户端密钥         │
│ ✅ 备用下载完成：晴天.mflac → 周杰伦 - 晴天.flac [FLAC，320kbps] 来源：kugou│
│   （勾选「显示详细信息」后，这里会补上逐音源排除统计/时长核验/降权说明）   │
├───────────────────────────────────────────────────────────────────────────┤
│                                                   [停止]  [   开始解密   ] │
└───────────────────────────────────────────────────────────────────────────┘
```

**区域说明**

- **顶部头区**：标题 + 一句话功能说明。
- **工具栏**：`添加文件 / 添加文件夹 / 移除所选 / 清空列表` + 目标音质下拉（MP3 默认 / FLAC / WAV）。
- **输出目录行**：手填或“选择…”；默认空表示“源文件所在目录”。
- **文件列表**：5 列；状态列按语义着色（绿=成功、蓝=备用下载、灰=跳过、红=失败）。整行可视，选中后可移除。
- **进度条**：按文件计数（`已完成/总数`），右侧文字同步。
- **日志区**：等宽字体，表情符号前缀让事件一目了然；自动滚动；行数上限防止长跑内存膨胀。
  **默认只显示"结论"**（`✅/❌/🔒/⚠`，单文件 1~2 行），过程明细折叠在左上角「显示详细信息」后面，
  右侧如实提示"已折叠 N 行详细过程"；勾选/取消即时重渲染。
- **底部动作**：`开始解密`（主色）、`停止`（危险色）。任务运行时禁用添加类按钮。

**交互细节**

- **拖拽**：把文件或文件夹拖进窗口即可添加；文件夹会递归展开并按支持的扩展名过滤。
- **多选**：列表支持多选后一次移除；不可编辑（避免误改）。
- **不卡 UI**：所有工作在后台线程，界面始终可响应；停止是协作式的（在检查点退出），已产出的文件保留。
- **完成汇总**：结束时日志打印 `成功 N · 跳过 N · 失败 N`。
- **自适应尺寸**：启动时按当前屏幕的**工作区**（已排除任务栏）计算尺寸与位置，并把标题栏/边框厚度一并算进去，确保**整窗**（含底部按钮行）完整可见；最小 820×520，可自由缩放。
- **几何记忆**：窗口大小/位置会被记住，但恢复时**自动收敛**到当前工作区内，换屏幕或换缩放比也不会把界面裁掉。

### 4.1.1 运行日志分级显示（v1.0.22）

同事（值班同学）不该被"已排除 8 个歌手不符的结果…"这类过程刷屏 —— 实测单个文件
**33~37 行**日志里有 30 行以上是过程明细。因此日志分两级，**只影响显示、不影响任何判定**：

* `_log(message, level)`：`level ∈ {"result", "detail"}`，**默认 `detail`**（新增日志不会污染结论区）；
* **结论级**（12 处显式标注）：`✅ 解密/备用下载完成`、`❌ 失败原因`、`🔒 需联网破解`、
  `⏭ 已跳过`、`⛔ 已取消`、`⚠ 离线解密失败`、以及**转码失败/引擎不可用**（产物不是目标格式必须可见）；
* **明细级**：逐音源排除统计、检索变体、时长核验与降权说明、实测码率、择优/去重过程；
* **兼容性**：`on_log` 的签名在构造时判断一次（`_accepts_level`）—— 两参数 sink（UI）收到 level，
  单参数 sink（CLI / `list.append` / 既有测试）**照旧收到全部行**，因此不需要改任何旧调用方；
* UI：`QPlainTextEdit` + 「显示详细信息」勾选框 + "已折叠 N 行"提示；重渲染走
  `setPlainText`（切换即时生效，不重跑任务）。冻结产物同样由 `--check-ui` 验收。

**接线事故（2026-10-20，已修并加回归）**：`inspect.signature` 对 **C 实现的可调用对象**
会抛 `ValueError` —— PySide6 的 `SignalInstance.emit`（`logged(QString,QString)`）正是如此。
第一版把"不可内省"当成"单参数"，于是 `emit(msg)` 抛 `TypeError`，又被 `_guarded` 吞掉：
**pipeline 的日志一条都到不了界面**（只剩界面自己写的 3 行），用户的观感就是
"勾不勾「显示详细信息」都没区别"。现在 `_accepts_level()` 返回**三态**
（`True` 两参数 / `False` 单参数 / `None` 不可内省），`None` 时"先试两参数、再退一参数"；
`ui/worker.py:make_log_sink()` 显式写成两参数 lambda，不再依赖"猜参数个数"。
回归覆盖：`selftest` 7 项（含**真实 Qt 信号**接线）+ `--check-ui` 5 项（含"经信号投递的日志也能分级"）。
教训：**容错层（`_guarded`）会把接线错误变成静默丢数据**，所以这类"静默"必须由自检盯着。

### 4.2 设置对话框

```text
┌─────────────── 设置 ───────────────┐
│ 备用音源（按顺序尝试）              │
│   [✓] 本地曲库（离线，优先）        │
│   [✓] 酷我音乐                      │
│   [✓] 酷狗音乐                      │
│   [✓] 全网聚合（MyFreeMP3/Netease） │
│ 网络与通用                          │
│   请求超时 [12.0 秒]  重试次数 [3]   │
│   最低码率 [128 kbps]               │
│   聚合接口地址 [music-api...api.php]│
│   聚合默认平台 [netease]            │
│ 本地曲库目录（可选，离线优先命中）   │
│   [目录列表………………]   [添加][移除]  │
│ 行为                                │
│   [✓] 解密失败时自动搜索下载备用音源 │
│   [✓] 写入元数据                    │
│   [✓] 在线补齐封面                  │
│   [ ] 同名文件直接覆盖              │
│   [✓] 保留原始加密文件              │
│   [✓] 启用哈希去重缓存              │
│  仅限个人本地研究使用，请尊重版权。  │
│                       [确定] [取消]  │
└────────────────────────────────────┘
```

**自适应**：对话框主体放在 `QScrollArea` 中，`确定 / 取消` 底栏与免责声明**固定在最下方**——`QDialog` 无法缩小到小于其布局最小高度，早期版本正因如此把底栏挤出了屏幕；打开时按屏幕工作区自适应尺寸并居中到主窗口。

设置持久化到 `%APPDATA%\MusicDecryptor\settings.json`（便携场景也可改成相对路径）；去重台账在 `hashcache.json`。

### 4.3 窗口与对话框自适应（共享实现）

**问题**：早期版本给主窗口写死 `resize(1060, 720)`、给设置对话框写死 `resize(600, 660)`。两个坑叠在一起：

1. `resize()` 设定的是**客户区**（不含标题栏/边框）；在 200% 缩放（逻辑工作区只有 1280×752）或任务栏较高时，加上外框就超出工作区，最底部一行（主窗口的 `停止 / 开始解密`、对话框的 `确定 / 取消`）被截断。
2. `QDialog` **无法缩小到小于自身布局的最小高度**，所以写死的 `resize(600, 660)` 会被内容顶开，底部按钮被挤出屏幕——这正是设置弹窗被截断的原因。

**做法**（统一收敛到 `ui/geometry.py`，主窗口与对话框共用，避免再各自为政）：

- `fit(widget, preferred, minimum, center_on)`：目标客户区 = 工作区 − **实测边框厚度**（`frameGeometry() − geometry()`）− 8 px 余量，再取 `min(preferred, 该值)`；然后把**外框**居中放进工作区。
- `clamp(widget)`：任何来源的几何只要外框越界，就收缩/平移回来（最大化/全屏时跳过）。
- 尺寸计算一律放在 `showEvent()`——此时窗口装饰才存在、量得准；`__init__` 只给一个先用尺寸。
- **主窗口**：最小 820×520，表格/日志设最小高度；`QSettings` 记忆几何，恢复后**必定**再 `clamp()` 一次。
- **设置对话框**：主体放进 `QScrollArea`，`确定 / 取消` 底栏与免责声明**固定在最下方**（即使不滚动也能直接点确定）；最小 460×380，打开时居中到主窗口。

**验证**：断言集中在 `ui/selfcheck.py`，**源码与冻结 exe 共用同一套检查**：

```powershell
python main.py --check-ui                  # 源码
.\dist\MusicDecryptor.exe --check-ui       # 冻结产物（退出码 0 = 全部通过）
```

10 项检查（全部 PASS）：主窗口外框落在工作区内、底部操作行与日志区未被裁切、超大历史几何被收敛；对话框外框落在工作区内、`确定/取消` 底栏未被裁切、OK 按钮落在工作区内、主体可滚动（内容不丢失）、缩到最小尺寸底栏仍可见。
本机实测（工作区 1280×752，即 2560×1504 @200%）：主窗口外框 1080×744 @(100,4)，对话框外框 620×744 @(326,0)。

---

## 5. 打包与图标配置

### 5.1 安装依赖

```powershell
python -m pip install -r requirements.txt
# 等价于： PySide6  mutagen  pillow  pyinstaller
```

### 5.2 生成图标（自定义 .ico）

`assets/make_icon.py` 用 Pillow 以 4× 超采样绘制：渐变圆角底板 + 白色八分音符 + 右下角“解锁”徽章（暗示“解密”）。一次导出多尺寸，Windows 各场景（任务栏/文件资源管理器/标题栏）都清晰：

```python
ICO_SIZES = [(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)]
img.save(OUT/"icon.ico", sizes=ICO_SIZES)
```

```powershell
python assets\make_icon.py     # 产出 assets\icon.ico 与 assets\icon.png
```

### 5.3 PyInstaller 规格（`build/music_decryptor.spec`）

要点：

- **单文件**：`Analysis → PYZ → EXE(..., a.binaries, a.datas)`，不生成 `COLLECT` 目录。
- **窗口程序**：`console=False`（不弹黑框）；同时提供 `music_decryptor_console.spec`（`console=True`）作为排障用的控制台版。
- **自定义图标**：`icon=os.path.join(ROOT,"assets","icon.ico")`。
- **资源内置**：`datas=[(icon,"assets")]`，运行时用 `mmusic/paths.py:resource_path()` 从 `sys._MEIPASS` 取。
- **裁剪体积**：`excludes` 去掉 `QtWebEngine*`、`QtQuick/Qml`、`QtMultimedia`、`Qt3D*`、`QtCharts`、`QtSql`、`QtTest`、`QtSvg`、`QtNetwork`、`tkinter`、`numpy` 等，只保留 `QtCore/QtGui/QtWidgets`。
- **hiddenimports**：显式声明 `mutagen.*` 子模块（mutagen 是动态导入，PyInstaller 静态分析可能漏掉）。
- 路径解析用 PyInstaller 注入的 `SPECPATH`，不再依赖 `__file__`。

### 5.4 一键构建

```powershell
# 方式 A：脚本（推荐）
powershell -ExecutionPolicy Bypass -File build\build.ps1        # 生成图标 → 校验依赖 → 构建

# 方式 B：直接调用
python assets\make_icon.py
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build\work build\music_decryptor.spec
```

产物：**`dist\MusicDecryptor.exe`**（本机实测约 46 MB，Windows 10/11 x64 免安装运行）。

### 5.5 构建后的验证

```powershell
# 0) 首要回归项：确认冻结后 Qt 能加载（不弹“找不到指定的程序”）
.\dist\MusicDecryptor.exe --check-qt          # 应快速退出且无对话框；控制台版会打印 "Qt 6.12.0 OK"
# 0b) 确认主窗口与设置弹窗都在本机屏幕上完整可见（退出码 0 = 通过）
.\dist\MusicDecryptor.exe --check-ui
# 1) 无界面链路
.\dist\MusicDecryptorConsole.exe --cli .\某文件.ncm --output .\out --no-fallback
# 2) GUI 版（应出现标题为“音乐解密工具 · Music Decryptor”的窗口）
.\dist\MusicDecryptor.exe
```

### 5.6 已踩过的三个打包坑（已修复，记录以免复发）

- **过度 exclude 导致 `ModuleNotFoundError: No module named 'email'`**：`urllib.request` 依赖标准库 `email`。曾把 `email/http.server/html` 一起排除 → 冻结后 `import urllib.request` 直接崩。**教训：`email` 绝不能排除**。
- **窗口模式异常 = 卡死**：`console=False` 时未捕获异常会弹出一个**模态错误框**并**阻塞进程**（表现为“CPU 近 0、迟迟不退出”）。排障办法就是构建 `console=True` 的变体，让 traceback 打到控制台。此外把“日志/进度回调”全部 `_guarded()` 包裹，从根上避免回调异常拖垮任务。
- **PATH 上的同名 DLL 被误打包（最隐蔽的一个，也是本工具首个发布版的致命 bug）**：
  PyInstaller 解析 `Qt6Core.dll` 的依赖 `icuuc.dll` 时，在 `PATH` 上匹配到了 **Poppler 自带的 ICU 78** 并打进了包。
  但 `Qt6Core.dll` 导入的是**无版本号**的 `ucnv_open`，只有 **Windows 系统目录的 `icuuc.dll`**（36 KB 的转发层）才导出它；Poppler 那份只导出 `ucnv_open_78`。
  结果：打包版一运行就 `ImportError: DLL load failed while importing QtCore: 找不到指定的程序`（即 `ERROR_PROC_NOT_FOUND`），**而源码环境因为用的是 System32 的 ICU 反而完全正常**——所以只测源码是发现不了的。
  **修复**：在 spec 里对 `a.binaries` 过滤掉所有 `icu*.dll`（Windows 10 1703+ 系统自带 ICU，本就无需打包）。顺带把包体从约 59 MB 降到约 46 MB。
  **教训**：`--cli` 路径**不 import Qt**，所以“用 `--cli` 跑通”不等于 exe 可用；冻结后**必须**单独验证 GUI 路径。为此本项目加了 `--check-qt` 自检开关，并额外用“进程主窗口标题”验证真实窗口确实创建。
  **通用防范**：打包机的 `PATH` 越干净越好；必要时用 `pefile` 校验关键 DLL 的导入/导出是否自洽（本次正是这样定位的）。

---

## 6. 使用说明与注意事项

### 6.1 命令行

`--query "歌名"` / `--query-file 歌单.txt` → 以名找歌（不需要输入文件，走备用音源链路）。

```powershell
python main.py                      # 启动图形界面
python main.py --cli "D:\in"        # 无界面批处理（目录递归）
python main.py --cli a.ncm --output D:\out --quality flac --no-fallback
python main.py --check-qt            # 自检：只加载 Qt 并打印版本（排查冻结后界面无法启动）
python main.py --check-ui            # 自检：主窗口与设置弹窗的屏幕自适应（本机实测 10/10）
python main.py --selftest           # 运行自检（离线）
python main.py --selftest --live    # 附：跑通真实备用下载链路
python main.py song.ncm --auto      # 启动 GUI 后立即开始
```

### 6.2 图形界面

1. `添加文件` / `添加文件夹`，或直接把文件拖进窗口。
2. 选目标音质（默认 MP3）；需要时填输出目录。
3. 点 `开始解密`，看列表“状态/输出文件”与下方日志。
4. 需要时在 `设置…` 里开关音源、调超时/重试/最低码率。

### 6.3 容错与用户可读提示

| 情形 | 行为 / 提示 |
|---|---|
| 网络瞬时失败 | 自动指数退避重试（默认 3 次），失败再切下一个音源 |
| 某音源 404/403/反爬 | 记为“不可用”，**自动切换下一个音源** |
| 所有音源均无结果 | `搜索无结果，请检查网络或歌曲名` |
| 有候选但均不可用 | `候选音源均不可用（无版权/校验失败/反爬），请更换关键词或手动补全直链` |
| 单个文件异常 | 记录 `失败` 与该文件原因，**不影响整批** |
| 文件损坏 | `无法识别的加密格式` / `无法定位 NCM 音频数据区` 等具体说明 |

### 6.4 边界情况处理（对照需求）

- **哈希去重**：每个输入算 SHA-256；命中缓存且产物仍在 → 直接 `已跳过`，不重复解密/下载。
- **备用搜索失败**：提示“是否更换关键词或人工补全直链”（日志给出该换词指引；聚合接口地址可自行改为自建网关，以“人工补全”能力）。
- **伴奏/改编版排除**：歌名、文件名与标签字段命中 `伴奏` / `inst` / `remix` / `cover` / `live` / `现场` / `演唱会` / `串烧` 等标记的候选**一律排除**（详见 §3.8）；只有当输入文件名自己带这些字样时才保留并警告。
- **跨音源择优**：所有音源的干净候选统一比排名，分高者先下，不再“第一个能用的音源就下”。
- **特殊字符 / 多版本 / 最佳匹配**：歌名归一化 + 打分排序 + 时长接近度；`歌手 - 歌名` 与 `歌名 - 歌手` **两种顺序都会尝试**（取匹配分更高者），单段文件名 `歌名` 也能解析；括号噪声（Live/Remaster/版本号）会被弱化但仍参与匹配——因此括注版本可能与正版同分（见 §3.7 已知弱点）。
- **404/403/反爬**：见 6.3，逐源切换。
- **元数据保留**：NCM 内嵌元数据（标题/艺术家/专辑/封面 URL）优先；**离线解密的其它格式（QMC/MGG/FLAC/Ogg）回读源文件自带标签与内嵌封面**（不联网、不凭空造图）；仍缺失时若走了备用下载，则由搜索结果补齐（封面仅在 `fetch_cover` 打开时在线抓取）。元数据**只写入音频文件自身**，不产生任何附带文件（早期版本的 `.meta.json` 侧车已移除）。
- **已知事实（不是 bug）**：QQ 下载的 `.mgg`（Ogg Vorbis）本体**不含内嵌封面**（实测 12 个样本 `pictures=0`，文件内搜不到 JPEG/PNG 签名），但**含正确的 `title/artist/album`**——所以“解出来没封面”是源文件的属性；要让这类文件带封面只能联网补，本工具**默认不做**（避免为每首歌多花一次网络等待）。
- **默认输出目录**：未指定输出目录时，产物写到**源文件所在目录**（与界面提示一致），**不再**落到进程工作目录（双击 exe 时即 `dist\`）。同名冲突自动加 `(1)`，**绝不覆盖源文件**；若输入本身已是明文且目标名与源文件同名，则直接跳过复制。源目录不可写时给出可读提示（引导改用其它目录），而不是崩溃。

### 6.5 合法性与版权

- 仅处理**你本机已有的**加密文件，或从你自己的备份恢复；**仅供个人研究**。
- 项目不含任何音频内容、不含任何账号凭据；不绕过平台付费/授权，仅解码本地文件容器。
- 请遵守当地法律与平台服务条款；**请勿分发解密后仍受版权保护的作品**。

---

## 附录 A · 支持的输入格式

- **网易云**：`.ncm`
- **QQ 音乐 QMCv1（静态）**：`.bkcmp3 .bkcm4a .bkcflac .bkcwav .bkcape .bkcogg .bkcwma .tkm` 及十六进制命名变体（`666c6163` 等）
- **QQ 音乐 QMCv2（密钥）**：`.qmc0 .qmc2 .qmc3 .qmc4 .qmc6 .qmc8 .qmcflac .qmcogg`
- **QQ 音乐 MGG/MFLAC**：`.mgg .mgg0 .mgg1 .mggl .mflac .mflac0 .mmp4`
  - 尾部 `QTag`（内嵌密钥）→ 离线解密；尾部 `STag`（无密钥）→ 标记“需联网破解”并转备用下载
- **已是明文**：`.mp3 .flac .ogg .m4a .aac .wav .ape .wma .dff`（直接复制并打标签）
- 输出扩展名一律以**解密后真实文件头**为准。

## 附录 B · 备用音源现状与维护建议

| 音源 | 搜索 | 直链 | 备注 |
|---|---|---|---|
| 本地曲库 | 本地 | `file://` | 完全离线，优先级最高 |
| 酷我 | 可用 | 可用（`anti.s`） | 实测可返回 CDN mp3 直链；伪 JSON 需专用解析器 |
| 酷狗 | 可用 | 免费曲目可用 | 付费/地区限制曲目 `url` 为空 → 自动跳过 |
| 聚合（MyFreeMP3/Netease） | 可用 | 可用（320kbps / 封面 / 歌词） | 地址与平台可配置，便于切换自建网关 |

维护宗旨：**平台改动只影响对应适配器**；解密核心与界面零改动。

## 附录 C · 自检结果（本机实测）

`python tests/selftest.py`：

- **离线用例 92/92 通过**，覆盖：AES（FIPS-197 向量）、TEA 往返、腾讯 TEA-CBC 加解密互逆、NCM 容器端到端字节级一致（含多艺术家/封面 URL 升级）、QMCv1 静态往返、QMCv2 映射往返、QMCv2 RC4 往返、`STag` 判定为“需联网”、下载校验（拒绝 HTML/过小/头不符）、歌名解析与排序、流水线四态（解/降级/复制/失败）与哈希去重、**默认输出目录回归**（落在源文件目录 / 显式目录优先 / 同名去重 / 源文件不被改写）、**输出文件整洁性**（不产生 `.meta.json` 侧车，且元数据仍写入音频文件）、**文件名双读法**（`歌名 - 歌手` 与 `歌手 - 歌名` 都能解析、查询词序保留、错误读法不再压低得分），以及一条**离线端到端**用例：`晴天 - 周杰伦.mgg`（STag、不可解码）经备用搜索→排序→本地音源→校验→落盘全链路成功；另有**版本过滤**（24 个必中标记 / 11 个必经正常标题 / 字段级检查 / 库里有伴奏也选原曲 / 文件名点名要伴奏则保留并警告）与**跨音源择优**（靠后的高分原曲必须战胜靠前的弱候选）共 14 项；再追加 **27 项时长链路**：FLAC / MP3-Xing / WAV 解析、假同步字防误判、参考时长选取（含两候选互斥、全无时长、单候选）、端到端“库里同时有 4:29 正片与 2:00 劣质剪辑时选正片”、“15 秒片段被下限拦下且不落盘”、“取不到时长则标注不可信”（单源兜底路径）；以及**跨平台参考时长**（酷我式 2:22 与酷狗式 3:55 同时出现时必须取 3:55 并淘汰 2:22）与**只下载通过时长验证的资源**（有参考时长却测不出时长时拒绝落盘）。
- `python tests/selftest.py --live`：**在线链路通过**——酷狗 5 条结果，成功解析直链、下载并通过文件头/码率校验（128 kbps）。
- `python tests/test_gui.py`：**Qt 离屏冒烟测试通过**——后台线程跑完、表格更新为“已解密”、产物落盘。
- `python tests/test_ui_fit.py`（等价于 `--check-ui`）：**真实屏幕 UI 适配测试通过（10/10）**——主窗口外框落在工作区内、底部操作行与日志区未被裁切、超大历史几何被自动收敛；设置对话框外框落在工作区内、`确定/取消` 底栏未被裁切、OK 按钮可见、主体可滚动、缩到最小尺寸底栏仍可见。
- 冻结产物：`dist\MusicDecryptor.exe` 以 `--cli` 处理合成 NCM，**退出码 0、产物字节与输入一致**；`--check-qt` 快速退出 0；启动后**真实窗口标题**为“音乐解密工具 · Music Decryptor”。

## 附录 D · 兼容性矩阵与发布验收清单

发布前逐项勾选；**任一项不通过即不发布**。带 ✅ 的是已有自动化覆盖，其余为需要人工/脚本补的项。

### D.1 运行环境

- ✅ 冻结产物 `--check-qt`、`--check-ui` 通过（无弹窗、退出码 0）。
- ⬜ 在**未安装 Visual C++ 运行库**的全新 Windows 10 21H2 / 22H2 与 Windows 11 上启动成功。
- ⬜ 在 `PATH` 含同名冲突 DLL 的机器上不崩（用 `pefile` 审计 bundle 内 DLL 的导入导出自洽性）。
- ✅ 无 `mutagen` 时仍能完成解密（仅不写标签）。
- ⬜ 无网络 / 断网环境下，离线解密路径完全可用。
- ⬜ 杀软误报检查（不启用 UPX；图标与版本信息完整）。

### D.2 显示与输入设备

- ✅ 真实屏幕工作区自适应：窗口与弹窗外框不越界、底部操作行不被裁切。
- ⬜ 100% / 125% / 150% / 200% 缩放各跑一遍。
- ⬜ 双屏且两块屏 DPI 不同时，跨屏拖动与重启后不越界。
- ⬜ 任务栏置于上/下/左/右四边时不遮挡内容。
- ⬜ 低分辨率（1024×768、1280×720）下最小尺寸仍可用。

### D.3 路径与文件名

- ⬜ 路径长度 > 260 字符（需长路径感知）。
- ⬜ 文件名含中文、空格、emoji、全角符号、`&`、括号。
- ⬜ 输出目录只读 / 无权限时给出可读提示而非崩溃。
- ⬜ UNC / 映射网络盘 / OneDrive 占位文件（按需下载）。

### D.4 输入容错（不得崩溃，必须可读报错）

- ⬜ 被截断或尾部损坏的 NCM / QMC 文件。
- ⬜ 扩展名与真实内容不符（例如把 `.ncm` 改名成 `.mp3`）。
- ⬜ 零字节文件、超大文件（> 200 MB 的 FLAC）。
- ⬜ 已是明文的音频（应跳过复制，不产生 `(1)` 副本）。

### D.5 输出兼容性

- ✅ 输出扩展名以**解密后真实文件头**为准，而非音源声明的扩展名。
- ✅ 元数据写入音频文件自身（不产生附加文件）。
- ⬜ ID3 采用 **v2.3**、不写入非常见标签帧，保证老播放器/车载兼容。
- ⬜ 保持原始采样率与位深，**不做隐式重采样或转码**。
