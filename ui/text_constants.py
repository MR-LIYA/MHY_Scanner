# ui/text_constants.py
"""独立静态文案，更新日志、提示文本全部放这里，单独维护"""

# MID缺失弹窗提示文字
MID_HELP_TEXT = (
    "当前账号缺少 MID，无法获取登录凭证！\n\n"
    "请输入 MID 后再试。获取 MID 的方法：\n"
    "1. 浏览器打开 https://user.mihoyo.com\n"
    "2. 登录后，地址栏中 ?login_ticket=... 之后的数字串就是 MID\n"
    "3. 也可以尝试 https://api-takumi.mihoyo.com/account/wapi/getUserInfo?stoken=你的stoken\n"
    "（响应中的 data.user_info.aid 即为 MID）\n\n"
    "添加 MID：右键账号 → 编辑MID"
)

# 更新日志：列表，按【最新在前】排序，每项包含版本标题 + html内容
CHANGELOG_LIST = [
    {
        "version_title": "v1.0.6 (2026-07)",
        "html": """
        <ul>
            <li>解决了更新程序的时候会偶发提示“由于与 64 位版本的 Windows 不兼容，此程序或功能"C:\\Users\\用户名\\AppData\\Local\\Temp\\MHY_Scanner Setup.exe"无法启动或运行。请联系软件供应商询问是否有与 64 位 Windows 兼容的版本。”的问题。</li>
            <li>解决了下载更新的时候可能会触发下载失败的问题。</li>
        </ul>
        """
    },
    {
        "version_title": "v1.0.5 (2026-07)",
        "html": """
        <ul>
            <li>暂时禁用了验证码登陆选项，验证码登录只能登陆程序，扫码时会有问题。</li>
        </ul>
        """
    },
    {
        "version_title": "v1.0.4 (2026-07)",
        "html": """
        <p>近期改动聚焦「严格对齐 C++ 版 `src` 的 api 与扫码两个模块」，修复了直播流扫码无法打开、以及官服扫码缺头导致校验不稳定的问题。</p>
        <ul>
            <li>
                <strong>直播流扫描（`scanner/scanner.py` `StreamScanner`）</strong>：
                <ul>
                    <li>由原来直接用 `cv2.VideoCapture` 打开直播流，改为<strong>优先使用 FFmpeg 子进程管道</strong>（`ffmpeg -i ... -f rawvideo`）读取帧，对应 C++ `QRCodeForStream::setUrl` / `avformat_open_input` 的实现。</li>
                    <li>新增 `set_headers()`，按平台注入 HTTP 头。对齐 C++ `WindowMain::GetStreamLink`：B 站流必须带 `User-Agent` / `Referer: https://live.bilibili.com/` / `Origin: https://live.bilibili.com`，否则 `bilivideo.com` CDN 返回 403、OpenCV 无法打开流（原「无法打开直播流」报错的根因）。</li>
                    <li>对齐 C++ 的 FFmpeg 低延迟选项：`rw_timeout=5000000`、`probesize=1024`、`max_delay=0`、`+nobuffer` / `low_delay`。</li>
                    <li>帧统一缩放为 1280×720 供二维码检测；停止扫描时正确 `terminate` FFmpeg 子进程，避免残留。</li>
                    <li>保留 `cv2.VideoCapture` 作为系统无 FFmpeg 时的回退路径。</li>
                </ul>
            </li>
            <li>
                <strong>直播流平台头（`ui/main_window.py`）</strong>：
                <ul>
                    <li>当平台为 BiliBili 时，自动注入配套请求头解决403拦截。</li>
                </ul>
            </li>
            <li>
                <strong>官服扫码头（`api/api.py` `panda_scan_qrcode`）</strong>：
                <ul>
                    <li>补齐 C++ 必带 `x-rpc-app_id`、`x-rpc-device_id` 请求头，正常获取 passport 登录链接。</li>
                </ul>
            </li>
            <li>支持米哈游客户端云游戏扫码登录（网页云游戏暂不兼容）</li>
        </ul>
        <p style="color:#666;font-size:13px;">说明：所有接口逻辑与原版C++扫码器完全对齐，无账号协议修改。</p>
        """
    },
    {
        "version_title": "v1.0.3 (2026-06)",
        "html": """
        <ul>
            <li>修复短信登录RSA加密偶发失败问题</li>
            <li>新增自动兑换stoken逻辑，无需手动转换ticket</li>
            <li>优化日志分级显示，区分DEBUG/INFO/WARN/ERROR</li>
            <li>修复直播间ID数字输入校验BUG</li>
        </ul>
        """
    },
    {
        "version_title": "v1.0.2 (2026-05)",
        "html": """
        <ul>
            <li>新增B服崩坏3完整扫码确认流程</li>
            <li>窗口置顶、启动自动监视开关</li>
            <li>账号备注编辑功能</li>
            <li>修复二维码轮询超时误判过期</li>
        </ul>
        """
    }
]
