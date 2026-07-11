# MHY_Scanner_py

米哈游扫码登录器（Python 版），基于 PyQt6 开发的米哈游游戏账号扫码登录工具。

参考 [Theresa-0328/MHY_Scanner](https://github.com/Theresa-0328/MHY_Scanner)（C++ 版）改进而来，修复了已知 BUG，优化了屏幕监视功能的稳定性。

> **最新版本**: v1.0.4
>
> **下载地址**: [Releases](https://github.com/MR-LIYA/MHY_Scanner/releases/download/main/MHY_Scanner_Setup.exe)

> **项目主页**: [https://github.com/MR-LIYA/MHY_Scanner](https://github.com/MR-LIYA/MHY_Scanner)
首次运行时需要等待一会，以便产生对应的配置文件。

---

## 功能特性

- **扫码登录**：基于 hoyolab Passport API，米游社 APP 扫码后确认即返回 Token，无需额外转换步骤
- **短信登录**：支持手机号 + 验证码登录
- **Cookie 登录**：支持粘贴 SToken Cookie 直接登录（stuid + stoken + mid）
- **B站崩坏3登录**：支持 BiliBili 服账号密码登录
- **Cookie 刷新**：支持抖音/B站 Cookie 刷新（设置 → 刷新Cookie）
- **屏幕扫描**：自动监视屏幕中出现的游戏内二维码，一键确认登录
- **直播流扫描**：从 B站 / 抖音直播间实时检测二维码并自动登录
- **多账号管理**：支持多账号存储、默认账号标记、服务器和备注编辑
- **四种游戏**：崩坏3、原神、星穹铁道、绝区零
- **双服务器**：官服 + BiliBili 服（崩坏3）
- **自动二次确认**：扫描到二维码后自动确认登录（开关，默认打开）
- **自动退出**：登录成功后自动关闭程序（开关，默认关闭）
- **自动启动扫描**：打开程序后自动开始屏幕扫描（开关，默认关闭）
- **窗口置顶**：主窗口始终位于最前
- **内置配置编辑器**：JSON 语法高亮、行号显示、智能缩进、缩放
- **检查更新**：支持 GitHub Release 版本更新

---

## 支持的游戏与服务器

| 游戏 | 官服 | BiliBili 服 |
| ------ | :----: | :-----------: |
| 崩坏3 (Honkai Impact 3rd) | ✅ | ✅ |
| 原神 (Genshin Impact) | ✅ | ❌ |
| 星穹铁道 (Honkai: Star Rail) | ✅ | ❌ |
| 绝区零 (Zenless Zone Zero) | ✅ | ❌ |

---

## 系统要求

| 项目 | 要求 |
| ------ | ------ |
| 操作系统 | Windows 10 21H2及以上版本（主要测试环境） |
| Python 版本 | 3.8+ |
| 内存 | 建议 4GB+ |
| FFmpeg | 直播流扫描需要（加入系统 PATH） |
| Visual C++ 运行时 | 运行打包 exe 需要 [VC++ Redist](https://aka.ms/vs/17/release/vc_redist.x64.exe) |

---

## 安装（源码运行）

### 1. 克隆项目

```bash
git clone https://github.com/your-repo/MHY_Scanner.git
cd MHY_Scanner
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 可选依赖

- **curl_cffi**：用于绕过 WAF -3503 风控（扫码轮询成功率更高）

```bash
pip install curl_cffi
```

### 4. FFmpeg（直播流扫描）

从 [FFmpeg 官网](https://ffmpeg.org/download.html) 下载，将 `bin` 目录加入系统 PATH 环境变量。若系统版本在Windows10 21H2及更版本系统会自带，无需安装。

### 5. 运行

```bash
python main.py
```

---

## 使用说明

### 扫码登录

1. 启动程序，点击"扫码登录"
2. 选择游戏和服务器类型
3. 使用米游社 APP 扫描屏幕上显示的二维码
4. 在手机上点击"确认登录"，Token 自动获取，账号自动保存到列表

> 扫码登录基于 **hoyolab Passport API**（`passport-api.miyoushe.com`），确认登录后 Token 通过 `Set-Cookie` 直接返回，无需额外的 ticket 兑换步骤。

### 短信登录

1. 点击"短信登录"
2. 输入手机号和验证码
3. 验证通过后自动完成登录

### Cookie 登录

1. 点击"Cookie 登录"
2. 粘贴完整的 Cookie 信息（格式：`stuid=xxx; stoken=xxx; mid=xxx`）
3. 选择游戏和服务器，点击登录

### B站崩坏3 登录

1. 点击"B站崩坏3"
2. 输入 B站 账号和密码
3. 登录成功后账号自动保存

### 屏幕扫描（自动登录）

1. 在主窗口右侧选择直播平台和直播间号（留空则扫描屏幕）
2. 点击"开始监视"
3. 在游戏中打开扫码登录界面，程序会自动检测并确认登录

### 直播流扫描

1. 选择直播平台（B站 / 抖音）
2. 输入房间号
3. 点击"开始监视"
4. 程序会从直播流中检测二维码并自动确认

### 账号管理

- **默认账号状态标识**：选中账号后右键 → "设为默认账号"（左侧带圆点标记）
- **编辑备注**：双击备注列直接编辑
- **编辑 MID**：右键 → "编辑 MID"
- **选择/替换/移除编辑默认账号**：右键/顶部账号管理 → “添加/修改/移除默认账号”
- **删除账号**：右键 → "删除账号"

---

## 配置说明

配置文件位于 `Config/` 目录：

### config.json（应用设置）

| 配置项 | 类型 | 默认值 | 说明 |
| ------ | ---- | ------ | ---- |
| `auto_exit` | bool | `true` | 登录成功后自动退出 |
| `auto_login` | bool | `true` | 自动二次确认登录 |
| `auto_start` | bool | `false` | 启动后自动开始扫描 |
| `always_on_top` | bool | `true` | 窗口始终置顶 |
| `last_platform` | int | `0` | 上次使用的直播平台 |
| `room_id` | string | `""` | 上次输入的直播间号 |
| `log_output_mode` | string | `"console"` / `"file"` | 日志输出模式 |
| `log_level` | int | `0` / `2` | 日志级别（0=DEBUG, 1=INFO, 2=WARN, 3=ERROR） |
| `editor_font_cn` | string | `"Microsoft YaHei"` | 编辑器中文字体 |
| `editor_font_en` | string | `"Consolas"` | 编辑器英文字体 |
| `editor_font_size` | int | `12` | 编辑器字号 |

> **注意**：源码运行时默认 `log_output_mode: "console"` + `log_level: 0`（DEBUG），打包后默认 `"file"` + `2`（WARN）。

### userinfo.json（账号数据）

兼容 C++ 版 MHY_Scanner 格式，可通过环境变量 `MHY_USERINFO_PATH` 指向外部 C++ 项目的 `userinfo.json` 文件实现数据互通。

**设置方法**：

1. 按 `Win + R`，输入 `sysdm.cpl`，打开**系统属性**
2. 点击 **高级** → **环境变量**
3. 在**用户变量**中点击**新建**
4. 变量名填写：`MHY_USERINFO_PATH`
5. 变量值填写 C++ 项目中 `userinfo.json` 的完整路径，例如：`D:\MHY_Scanner\Config\userinfo.json`
6. 点击确定，重启程序后生效

---

## 项目结构

```text
MHY_Scanner/
├── main.py                     # 程序入口（版本号统一管理）
├── __init__.py                 # 包入口
├── requirements.txt            # Python 依赖清单
├── api/                        # API 层
│   ├── api.py                  # 米哈游 API 核心（hoyolab扫码 / 短信登录 / 游戏内二维码 / Token 交换）
│   └── bsgamesdk.py            # B站 SDK（崩坏3 B服 账号密码登录）
├── core/                       # 核心模块
│   ├── config.py               # 配置管理（单例，兼容 C++ userinfo.json）
│   └── logger.py               # 日志系统（控制台 / 文件输出，多标签分类）
├── scanner/                    # 扫描模块
│   ├── scanner.py              # 屏幕二维码扫描器 + 直播流扫描器
│   └── livestream.py           # 直播流获取（B站 / 抖音）
├── ui/                         # 用户界面
│   ├── main_window.py          # 主窗口（账号管理 / 扫描控制 / 菜单栏）
│   ├── login_window.py         # 登录窗口（4 种登录方式 Tab 页）
│   ├── account_manager.py      # 账号管理器
│   ├── add_account_dialog.py   # 手动添加账号对话框
│   ├── config_editor.py        # 内置配置文件编辑器（JSON 高亮 + 行号）
│   └── cookie_refresh_dialog.py # Cookie 刷新对话框（抖音/B站扫码刷新）
├── utils/                      # 工具模块
│   └── update.py               # 热更新管理器
├── hooks/                      # PyInstaller 打包钩子
│   ├── hook-curl_cffi.py
│   ├── hook-PIL.py
│   └── hook-pyzbar.py
├── Config/                     # 配置文件目录
│   ├── config.json             # 应用设置（自动生成）
│   ├── cookie.json             # 抖音/B站 Cookie 存储（自动生成）
│   └── userinfo.json           # 账号数据（自动生成）
└── log/                        # 日志目录（自动生成）
```

---

## 编译打包

### 使用 PyInstaller

```bash
pip install pyinstaller
pyinstaller --onefile --windowed \
    --name="MHY_Scanner" \
    --icon=icons/app.png \
    --add-data="Config;Config" \
    --add-data="icons;icons" \
    --hidden-import=curl_cffi \
    --hidden-import=pyzbar \
    --hidden-import=PIL \
    --additional-hooks-dir=hooks \
    main.py
```

### 打包注意事项

1. **curl_cffi** 为可选依赖，打包时建议包含以提升扫码轮询成功率
2. 使用 `hooks/` 目录下的钩子文件确保相关子模块被正确收集
3. 打包后日志默认输出到文件（`log/` 目录），而非控制台

### 已打包版本

预编译的 `.exe` 文件可直接运行，无需安装 Python 环境：

1. 下载并安装 `MHY_Scanner.exe`
2. 确保系统已安装 [VC++ Redist 运行时](https://aka.ms/vs/17/release/vc_redist.x64.exe)
3. 直播流扫描需要安装 FFmpeg 并加入 PATH

---

## 注意事项

### 使用注意

1. **米游社 APP**：扫码登录需要在手机上安装米游社 APP 并已登录目标账号
2. **B站崩坏3**：仅支持崩坏3 BiliBili 服
3. **直播流扫描**：需要先在系统上安装 FFmpeg，并确保非免流直播间
4. **网络环境**：需要能够正常访问米哈游 API（`api-sdk.mihoyo.com` 等域名）
5. **配置编辑器**：使用内置配置编辑器修改配置文件前建议备份，格式错误可能导致程序异常

### WAF 风控

- 米哈游对 API 请求有 WAF 风控（错误码 `-3503`）
- **扫码登录**（自生成二维码）：使用 **hoyolab Passport API**（`passport-api.miyoushe.com`），WAF 策略更宽松，轮询稳定
- **屏幕扫描**（游戏内二维码）：使用 hk4e-sdk 端点，仍可能触发 WAF
- 推荐安装 `curl_cffi` 库模拟真实浏览器 TLS 指纹绕过风控
- 轮询间隔已加入 0~500ms 随机抖动，避免触发频率限制

### C++ 版兼容

- `userinfo.json` 格式与 C++ 版 [Theresa-0328/MHY_Scanner](https://github.com/Theresa-0328/MHY_Scanner) 兼容
- 设置环境变量 `MHY_USERINFO_PATH` 指向 C++ 版项目的 `userinfo.json` 文件路径即可实现账号数据共享（详细步骤见上方配置说明）
- `type` 字段映射：`"官服"` → 官服/原神，`"崩坏3B服"` → B服/崩坏3

### 日志

- 日志文件位于 `log/` 目录
- 单文件上限 100KB，自动滚动归档，保留 7 天
- 源码运行时默认输出到控制台（DEBUG 级别），打包后默认输出到文件（WARN 级别）

---

## 依赖清单

| 依赖 | 最低版本 | 用途 |
| ------ | ------- | ------ |
| PyQt6 | ≥6.4.0 | GUI 框架 |
| PyQt6-WebEngine | ≥6.4.0 | Cookie 刷新浏览器内核 |
| opencv-python | ≥4.8.0 | 图像处理与二维码检测 |
| numpy | ≥1.24.0 | 数值计算 |
| requests | ≥2.31.0 | HTTP 请求 |
| Pillow | ≥10.0.0 | 图像处理 |
| mss | ≥9.0.1 | 跨平台屏幕截图 |
| cryptography | ≥41.0.0 | RSA 加密 |
| python-dateutil | ≥2.8.0 | 日期处理 |
| qrcode | ≥7.4.0 | 二维码生成 |
| ffmpeg-python | — | 视频流处理 |
| pyzbar | ≥0.1.9 | 二维码解码（优先使用） |

### 可选依赖

| 依赖 | 用途 |
| ------ | ------ |
| curl_cffi | 模拟真实 TLS 指纹绕过 WAF 风控 |

---

## 更新日志

### v1.0.4 (2026-07)

近期改动聚焦「严格对齐 C++ 版 `src` 的 api 与扫码两个模块」，修复了直播流扫码无法打开、以及官服扫码缺头导致校验不稳定的问题。

- **直播流扫描（`scanner/scanner.py` `StreamScanner`）**：
  - 由原来直接用 `cv2.VideoCapture(url)` 打开直播流，改为**优先使用 FFmpeg 子进程管道**（`ffmpeg -i ... -f rawvideo`）读取帧，对应 C++ `QRCodeForStream::setUrl` / `avformat_open_input` 的实现。
  - 新增 `set_headers()`，按平台注入 HTTP 头。对齐 C++ `WindowMain::GetStreamLink`：B 站流必须带 `User-Agent` / `Referer: https://live.bilibili.com/` / `Origin: https://live.bilibili.com`，否则 `bilivideo.com` CDN 返回 403、OpenCV 无法打开流（原「无法打开直播流」报错的根因）。
  - 对齐 C++ 的 FFmpeg 低延迟选项：`rw_timeout=5000000`、`probesize=1024`、`max_delay=0`、`+nobuffer` / `low_delay`。
  - 帧统一缩放为 1280×720 供二维码检测；停止扫描时正确 `terminate` FFmpeg 子进程，避免残留。
  - 保留 `cv2.VideoCapture` 作为系统无 FFmpeg 时的回退路径。
- **直播流平台头（`ui/main_window.py` `start_stream_scan`）**：当平台为 BiliBili 时，向 `StreamScanner` 写入与 C++ 一致的 `User-Agent` / `Referer` / `Origin` 头。
- **官服扫码头（`api/api.py` `panda_scan_qrcode`）**：补齐 C++ `PandaScanQRCode` 必发的 `x-rpc-app_id` 与 `x-rpc-device_id` 请求头，使 `panda/qrcode/scan` 返回有效的 `passport_qr_url`。

> 说明：上述改动均为与 C++ `src`（C++ 版 `MHY_Scanner`）逐字段对齐，不涉及账号/登录协议逻辑变更。

### v1.0.3 (2026-06)

- **新增 Cookie 刷新功能**：抖音直接点击刷新即可，B站需扫码登录提取登录态
- 当前版本验证码暂不支持 HarmonyOS 及 iOS

---

## 开源协议

本项目仅供学习交流使用，请勿用于非法用途。
## 参考项目：
- [Theresa-0328/MHY_Scanner](https://github.com/Theresa-0328/MHY_Scanner)  

- [loqwe/MHY_Scanner2](https://github.com/loqwe/MHY_Scanner2)
- [MR-LIYA/MHY_Scanner](https://github.com/MR-LIYA/MHY_Scanner)
--- 


## 致谢

- [Theresa-0328/MHY_Scanner](https://github.com/Theresa-0328/MHY_Scanner) — 原始 C++ 版项目
- [loqwe/MHY_Scanner2](https://github.com/loqwe/MHY_Scanner2) — C++ 二改版项目
- [MR-LIYA/MHY_Scanner](https://github.com/MR-LIYA/MHY_Scanner) — Python 版项目
- [PyQt6](https://www.riverbankcomputing.com/software/pyqt/) — Python GUI 框架
- [OpenCV](https://opencv.org/) — 计算机视觉库
- 所有贡献者和用户
