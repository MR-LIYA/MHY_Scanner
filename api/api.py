"""
米哈游API调用模块
"""
import json
import time
import random
import hashlib
import requests
from enum import IntEnum
from typing import Optional, Tuple, Dict, Any
from urllib.parse import urlencode
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from core.logger import api_log, qr_log, debug, info, warn, error, LogLevel

# 尝试使用 curl_cffi 模拟 libcurl TLS 指纹（与 C++ 一致），绕过 miHoYo WAF -3503
try:
    from curl_cffi import requests as curl_requests  # type: ignore
    _USE_CURL_CFFI = True
except ImportError:
    curl_requests = None  # type: ignore
    _USE_CURL_CFFI = False

def _safe_post(url: str, json_data: dict, headers: dict, timeout: int = 10):
    """HTTP POST 请求（对齐 C++ cpr::Post — 每次新建连接，无持久 Session）

    C++ 使用 cpr::Post()（大写P，free function），每次创建临时 Session → 独立连接。
    Python 之前用 curl_cffi.Session() 持久连接复用，TLS 会话恢复行为易被 WAF
    识别为持续轮询机器人。改为每次新建 Session 对齐 C++ 行为。
    """
    if _USE_CURL_CFFI:
        # 每次新建 Session = 对齐 C++ cpr::Post() 的上限 P 临时 Session
        return curl_requests.Session().post(url, json=json_data, headers=headers,
                                            timeout=timeout)  # type: ignore
    else:
        return requests.post(url, json=json_data, headers=headers, timeout=timeout)


def _safe_get(url: str, headers: dict, timeout: int = 10):
    """HTTP GET 请求（对齐 C++ cpr::Get — 每次新建连接，无持久 Session）"""
    if _USE_CURL_CFFI:
        return curl_requests.Session().get(url, headers=headers, timeout=timeout)  # type: ignore
    else:
        return requests.get(url, headers=headers, timeout=timeout)


class GameType(IntEnum):
    """
    游戏类型 → API 端点映射

    ┌───────────────┬──────┬──────────────────────────────────────────────────┐
    │ 游戏           │ ID   │ API 端点 (fetch / query / scan / confirm)          │
    ├───────────────┼──────┼──────────────────────────────────────────────────┤
    │ 崩坏3          │ 1    │ https://api-sdk.mihoyo.com/bh3_cn/combo/panda/... │
    │ 未定事件簿     │ 2    │ https://hk4e-sdk.mihoyo.com/hk4e_cn/combo/panda/..│
    │ 原神           │ 4    │ https://hk4e-sdk.mihoyo.com/hk4e_cn/combo/panda/..│
    │ 星穹铁道       │ 8    │ https://api-sdk.mihoyo.com/hkrpg_cn/combo/panda/..│
    │ 云游戏         │ 9    │ (未实现)                                          │
    │ 星穹铁道(备)   │ 11   │ (同 ID=8)                                         │
    │ 绝区零         │ 12   │ https://api-sdk.mihoyo.com/nap_cn/combo/panda/... │
    │ 崩坏3(B服)     │ 10000│ https://api-sdk.mihoyo.com/bh3_cn/combo/panda/... │
    └───────────────┴──────┴──────────────────────────────────────────────────┘

    端点域名规律:
      - hk4e-sdk.mihoyo.com  → 自生成二维码 (fetch + query)，仅 app_id=4 (原神) 及 app_id=2
      - api-sdk.mihoyo.com    → 游戏内二维码 (scan + confirm)，所有游戏
      - api-takumi.mihoyo.com → 通行证 token 交换 (stoken/game_token)
      - passport-api.mihoyo.com → 短信登录

    WAF 风控强度: app_id=1(高) > app_id=4(中) > app_id=2(低)
    """
    UNKNOW = 0
    Honkai3 = 1        # 崩坏3 → api-sdk/bh3_cn
    TearsOfThemis = 2  # 未定事件簿 → hk4e-sdk/hk4e_cn (WAF 最宽松)
    Genshin = 4        # 原神 → hk4e-sdk/hk4e_cn (fetch/query), api-sdk/hk4e_cn (scan/confirm)
    HonkaiStarRail = 8 # 星穹铁道 → api-sdk/hkrpg_cn
    CloudGame = 9      # 云游戏 (未实现端点)
    PJSH = 11          # 崩坏：星穹铁道 (备选ID)
    ZenlessZoneZero = 12  # 绝区零 → api-sdk/nap_cn
    Honkai3_BiliBili = 10000  # 崩坏3 BiliBili服 → api-sdk/bh3_cn (带 B站签名)


class ServerType(IntEnum):
    """服务器类型"""
    Official = 1   # 官服
    BiliBili = 2  # BiliBili服


class ScanRet(IntEnum):
    """扫描结果"""
    UNKNOW = 0
    SUCCESS = 1
    FAILURE_1 = 3  # 扫码失败
    FAILURE_2 = 4  # 确认失败
    LIVESTOP = 5   # 直播停止
    STREAMERROR = 6  # 流错误


class LoginQRCodeState(IntEnum):
    """二维码状态"""
    Init = 0
    Scanned = 1
    Confirmed = 2
    Expired = 3


# ═══════════════════════════════════════════════════════════════════
# API URL 定义 — 各游戏端点全集
# ═══════════════════════════════════════════════════════════════════

API_SDK = "https://api-sdk.mihoyo.com"

# ── 崩坏3 (app_id=1) ──────────────────────────────────────────
# 端点: api-sdk.mihoyo.com/bh3_cn/combo/panda/...
BH3_BASE = f"{API_SDK}/bh3_cn"
BH3_V2_LOGIN = f"{BH3_BASE}/combo/granter/login/v2/login"    # B服 login v2
BH3_QRCODE_SCAN = f"{BH3_BASE}/combo/panda/qrcode/scan"
BH3_QRCODE_CONFIRM = f"{BH3_BASE}/combo/panda/qrcode/confirm"
BH3_QRCODE_QUERY = f"{BH3_BASE}/combo/panda/qrcode/query"

# BH3 Bilibili OA 调度信息（对齐 C++ GetOAString）
BH3_BILIBILI_OA_URL = "https://api.v6qbb.cloud/get_bh3_bilibili_oa"

# ── 原神 (app_id=4) / 未定事件簿 (app_id=2) — 自生成二维码 ────
# 端点: hk4e-sdk.mihoyo.com/hk4e_cn/combo/panda/...
# 用于程序自己生成二维码的 fetch + query 流程
HK4E_SDK_BASE = "https://hk4e-sdk.mihoyo.com/hk4e_cn"
QRCODE_FETCH = f"{HK4E_SDK_BASE}/combo/panda/qrcode/fetch"    # 获取二维码
QRCODE_QUERY = f"{HK4E_SDK_BASE}/combo/panda/qrcode/query"    # 查询状态
QRCODE_SCAN = f"{HK4E_SDK_BASE}/combo/panda/qrcode/scan"
QRCODE_CONFIRM = f"{HK4E_SDK_BASE}/combo/panda/qrcode/confirm"

# ── 原神 (app_id=4) — 游戏内二维码 ─────────────────────────────
# 端点: api-sdk.mihoyo.com/hk4e_cn/combo/panda/...
# scan/confirm/query 必须用同一域名，否则返回 -105
HK4E_API_BASE = f"{API_SDK}/hk4e_cn"
HK4E_QRCODE_SCAN = f"{HK4E_API_BASE}/combo/panda/qrcode/scan"
HK4E_QRCODE_CONFIRM = f"{HK4E_API_BASE}/combo/panda/qrcode/confirm"
HK4E_QRCODE_QUERY = f"{HK4E_API_BASE}/combo/panda/qrcode/query"

# ── 星穹铁道 (app_id=8) ───────────────────────────────────────
# 端点: api-sdk.mihoyo.com/hkrpg_cn/combo/panda/...
HKRPG_SDK_BASE = f"{API_SDK}/hkrpg_cn"
HKRPG_QRCODE_QUERY = f"{HKRPG_SDK_BASE}/combo/panda/qrcode/query"
HKRPG_QRCODE_SCAN = f"{HKRPG_SDK_BASE}/combo/panda/qrcode/scan"
HKRPG_QRCODE_CONFIRM = f"{HKRPG_SDK_BASE}/combo/panda/qrcode/confirm"

# ── 绝区零 (app_id=12) ────────────────────────────────────────
# 端点: api-sdk.mihoyo.com/nap_cn/combo/panda/...
NAP_SDK_BASE = f"{API_SDK}/nap_cn"
NAP_QRCODE_QUERY = f"{NAP_SDK_BASE}/combo/panda/qrcode/query"
NAP_QRCODE_SCAN = f"{NAP_SDK_BASE}/combo/panda/qrcode/scan"
NAP_QRCODE_CONFIRM = f"{NAP_SDK_BASE}/combo/panda/qrcode/confirm"

# ── 通行证 / 账户 API ──────────────────────────────────────────
# 端点: api-takumi.mihoyo.com (token 交换, 与游戏无关)
TAKUMI_BASE = "https://api-takumi.mihoyo.com"
GAME_TOKEN = f"{TAKUMI_BASE}/auth/api/getGameToken"
GAME_TOKEN_STOKEN = f"{TAKUMI_BASE}/account/ma-cn-session/app/getTokenByGameToken"
MULTI_TOKEN = f"{TAKUMI_BASE}/auth/api/getMultiTokenByLoginTicket"
# 校验 stoken 是否有效（对齐 C++ CheckStokenValid: getCookieAccountInfoBySToken）
COOKIE_ACCOUNT_INFO = f"{TAKUMI_BASE}/auth/api/getCookieAccountInfoBySToken"

# ── 短信登录 ───────────────────────────────────────────────────
# 端点: passport-api.mihoyo.com
# 新版API: createLoginCaptcha(发送验证码) / loginByMobileCaptcha(验证登录)
PASSPORT_BASE = "https://passport-api.mihoyo.com"
SMS_CREATE = f"{PASSPORT_BASE}/account/ma-cn-verifier/verifier/createLoginCaptcha"
SMS_LOGIN = f"{PASSPORT_BASE}/account/ma-cn-passport/app/loginByMobileCaptcha"

# ── PandaScan + passport 扫码登录（对齐 C++ MhyApi.hpp / QRCodeForScreen） ──
# 屏幕/直播扫码官服流程使用：先 PandaScanQRCode 获取 passport 二维码，
# 再用已登录账号的 stoken/mid 完成 ScanPassportQRLogin + ConfirmPassportQRLogin
PASSPORT_PANDA_APP_ID = "bll8iq97cem8"
PASSPORT_SCAN_QR = f"{PASSPORT_BASE}/account/ma-cn-passport/app/scanQRLogin"
PASSPORT_CONFIRM_QR = f"{PASSPORT_BASE}/account/ma-cn-passport/app/confirmQRLogin"

# ── hoyolab / 米游社 扫码登录（新 passport API） ──────────────
# 端点: passport-api.miyoushe.com
# 确认登录后通过响应 Set-Cookie 直接返回 ltoken_v2/cookie_token，
# 无需 login_ticket → stoken 的额外转换步骤
PASSPORT_MIYOUSHE = "https://passport-api.miyoushe.com"
HOYOLAB_QR_CREATE = f"{PASSPORT_MIYOUSHE}/account/ma-cn-passport/web/createQRLogin"
HOYOLAB_QR_QUERY = f"{PASSPORT_MIYOUSHE}/account/ma-cn-passport/web/queryQRLoginStatus"
HOYOLAB_APP_ID = "bll8iq97cem8"  # x-rpc-app_id 固定值

# RSA 公钥拉取 URL（尝试多个已知端点）
RSA_KEY_URLS = [
    f"{PASSPORT_BASE}/account/ma-cn-passport/app/getRSAKey",
    f"{PASSPORT_BASE}/account/ma-cn-passport/app/get_by_rsa_key",
    f"{PASSPORT_BASE}/account/ma-cn-passport/web/rsa_public_key",
]

# ── 用户信息 ───────────────────────────────────────────────────
# 端点: bbs-api.miyoushe.com (米游社社区 API)
MYS_BASE = "https://bbs-api.miyoushe.com"
USER_INFO = f"{MYS_BASE}/user/api/getUserFullInfo"

# ── 常量 ───────────────────────────────────────────────────────

# 短信发送频率限制
_last_sms_send_time = 0
SMS_COOLDOWN_SECONDS = 60  # 发送间隔至少60秒

# DS 签名盐值
SALT_X6 = "t0qEgfub6cvueAPgR5m9aQWWVciEer7v"

# 设备ID (每次启动生成)
import uuid
DEVICE_ID = str(uuid.uuid4())
# 设备指纹（模拟固定值，对齐游戏客户端）
DEVICE_FP = "38d814469b1e4"


def get_sms_request_headers(lifecycle_id: str = "") -> Dict[str, str]:
    """获取短信登录专用请求头（对齐游戏客户端风格，无DS签名）"""
    if not lifecycle_id:
        lifecycle_id = str(uuid.uuid4())
    return {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "x-rpc-app_id": "c76ync6mutq8",
        "x-rpc-channel_id": "1",
        "x-rpc-channel_version": "2.49.0.189",
        "x-rpc-client_type": "3",
        "x-rpc-device_fp": DEVICE_FP,
        "x-rpc-device_id": DEVICE_ID,
        "x-rpc-device_model": "8BAB",
        "x-rpc-device_name": "LAPTOP-T17POR1K",
        "x-rpc-game_biz": "hk4e_cn",
        "x-rpc-language": "zh-cn",
        "x-rpc-lifecycle_id": lifecycle_id,
        "x-rpc-mdk_version": "2.49.0.189",
        "x-rpc-sdk_version": "2.49.0.189",
        "x-rpc-sys_version": "Windows%2011",
    }


def create_uuid4() -> str:
    """生成UUID4"""
    return str(uuid.uuid4())


def md5(text: str) -> str:
    """MD5哈希"""
    return hashlib.md5(text.encode()).hexdigest()


def hmac_sha256(key: str, message: str) -> str:
    """HMAC-SHA256"""
    import hmac
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


def generate_ds(body: str = "", query: str = "") -> str:
    """生成DS签名"""
    t = int(time.time())
    r = random.randint(100001, 200000)
    m = f"salt={SALT_X6}&t={t}&r={r}&b={body}&q={query}"
    return f"{t},{r},{md5(m)}"


def get_request_headers() -> Dict[str, str]:
    """获取请求头"""
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) miHoYoBBS/2.76.1",
        "Accept": "application/json",
        "x-rpc-app_id": "bll8iq97cem8",
        "x-rpc-app_version": "2.76.1",
        "x-rpc-channel": "mihoyo",
        "x-rpc-client_type": "2",
        "x-rpc-device_id": DEVICE_ID,
        "x-rpc-device_name": "",
        "x-rpc-game_biz": "bbs_cn",
        "x-rpc-sdk_version": "2.16.0",
    }


# RSA 公钥缓存
_rsa_key_cache: Optional[str] = None
_rsa_key_expiry: float = 0  # 缓存过期时间


def get_rsa_public_key() -> bytes:
    """获取RSA公钥 - 优先从服务器拉取最新公钥，失败则使用本地硬编码公钥"""
    global _rsa_key_cache, _rsa_key_expiry
    
    # 缓存未过期时直接返回
    if _rsa_key_cache and time.time() < _rsa_key_expiry:
        return _rsa_key_cache.encode()
    
    # 尝试从多个已知端点拉取最新公钥
    for key_url in RSA_KEY_URLS:
        try:
            import requests as _req
            resp = _req.get(key_url, timeout=5)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    pub_key = data.get("data", {}).get("public_key") or data.get("data", {}).get("rsa_key") or data.get("rsa_key") or data.get("public_key")
                    if pub_key:
                        _rsa_key_cache = f"-----BEGIN PUBLIC KEY-----\n{pub_key}\n-----END PUBLIC KEY-----"
                        _rsa_key_expiry = time.time() + 3600
                        api_log(f"[rsa_key] 已从 {key_url} 获取最新RSA公钥，缓存1小时", LogLevel.INFO)
                        return _rsa_key_cache.encode()
                except Exception:
                    # 可能是HTML或其它格式
                    text = resp.text
                    # 尝试从HTML中提取 public_key
                    import re
                    m = re.search(r'"public_key"\s*:\s*"([^"]+)"', text)
                    if m:
                        pub_key = m.group(1)
                        _rsa_key_cache = f"-----BEGIN PUBLIC KEY-----\n{pub_key}\n-----END PUBLIC KEY-----"
                        _rsa_key_expiry = time.time() + 3600
                        api_log(f"[rsa_key] 已从 {key_url} HTML中提取RSA公钥，缓存1小时", LogLevel.INFO)
                        return _rsa_key_cache.encode()
                    api_log(f"[rsa_key] {key_url} 返回非JSON内容", LogLevel.DEBUG)
            else:
                api_log(f"[rsa_key] {key_url} HTTP {resp.status_code}", LogLevel.DEBUG)
        except Exception as e:
            api_log(f"[rsa_key] {key_url} 异常: {e}", LogLevel.DEBUG)
    
    api_log(f"[rsa_key] 所有URL均无法获取公钥，使用硬编码公钥", LogLevel.WARN)
    
    # 回退：使用硬编码公钥
    public_key_pem = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDDvekdPMHN3AYhm/vktJT+YJr7
cI5DcsNKqdsx5DZX0gDuWFuIjzdwButrIYPNmRJ1G8ybDIF7oDW2eEpm5sMbL9zs
9ExXCdvqrn51qELbqj0XxtMTIpaCHFSI50PfPpTFV9Xt/hmyVwokoOXFlAEgCn+Q
CgGs52bFoYMtyi+xEQIDAQAB
-----END PUBLIC KEY-----"""
    return public_key_pem


def rsa_encrypt(data: str) -> str:
    """RSA加密"""
    public_key = serialization.load_pem_public_key(
        get_rsa_public_key(),
        backend=default_backend()
    )
    encrypted = public_key.encrypt(
        data.encode(),
        padding.PKCS1v15()
    )
    return base64.b64encode(encrypted).decode()


class MhyApi:
    """米哈游API调用类"""
    
    def __init__(self):
        self.session = requests.Session()
        self.device_id = DEVICE_ID
        self.last_ticket = ""
        self.game_type = GameType.Genshin
        self.server_type = ServerType.Official
    
    def set_game_type(self, game_type: GameType):
        """设置游戏类型"""
        self.game_type = game_type
    
    def set_server_type(self, server_type: ServerType):
        """设置服务器类型"""
        self.server_type = server_type
    
    def fetch_qrcode_url(self, game_type: Optional[GameType] = None) -> Tuple[str, str]:
        """
        获取二维码URL（对齐 C++ GetLoginQrcodeUrl — 仅 Content-Type 头，无状态请求）
        Returns: (qrcode_url, ticket)

        ticket 提取策略：
        - 对齐 C++: ticket = URL 末尾 24 字符
          C++ WindowLogin.cpp: string_view ticket(str.data() + str.size() - 24, 24);

        app_id 选择策略：
        - 自生成二维码（默认）: app_id=2 (TearsOfThemis)，hk4e-sdk WAF 最宽松
          app_id=1/4 在 ~5 次 query 轮询后触发 -3503 WAF → 二维码误判为过期
        - 游戏内扫码: 传入 game_type 参数使用对应 app_id (1/4/8/12)
        """
        if game_type is None:
            game_type = GameType.TearsOfThemis  # app_id=2, WAF 最宽松
        app_id = int(game_type)
        data = {
            "app_id": app_id,
            "device": self.device_id
        }
        # C++ fetch_qrcode 仅发 Content-Type，不带 User-Agent / x-rpc-* 头
        headers = {"Content-Type": "application/json"}

        try:
            resp = _safe_post(QRCODE_FETCH, json_data=data, headers=headers)
            result = resp.json()
            qr_log(f"[fetch_qrcode_url] response: {result}", LogLevel.DEBUG)

            if result.get("retcode", -1) == 0:
                qrcode_url = result["data"]["url"]
                qr_log(f"[fetch_qrcode_url] QR URL: {qrcode_url}", LogLevel.DEBUG)

                # 对齐 C++: URL 末尾 24 字符为 ticket
                if len(qrcode_url) >= 24:
                    ticket = qrcode_url[-24:]
                    qr_log(f"[fetch_qrcode_url] ticket(by [-24:]): {ticket}", LogLevel.DEBUG)
                else:
                    ticket = ""
                    qr_log(f"[fetch_qrcode_url] 无法提取ticket! URL={qrcode_url}", LogLevel.WARN)

                return qrcode_url, ticket
            else:
                qr_log(f"获取二维码失败: {result}", LogLevel.WARN)
                return "", ""
        except Exception as e:
            qr_log(f"请求异常: {e}", LogLevel.ERROR)
            return "", ""
    
    def query_qrcode_state(self, ticket: str, biz_key: str = "", game_type: Optional[GameType] = None) -> Tuple[LoginQRCodeState, str, str]:
        """
        查询二维码状态（用于程序自己生成的二维码）
        Returns: (state, uid, token)

        app_id 选择策略（与 fetch_qrcode_url 保持一致）：
        - 自生成二维码（默认）: app_id=2 (TearsOfThemis)，WAF 最宽松
        - 游戏内扫码: 传入 game_type 参数使用对应 app_id
        """
        if game_type is None:
            game_type = GameType.TearsOfThemis  # app_id=2, WAF 最宽松
        app_id = int(game_type)
        return self._query_qrcode_state_impl(ticket, app_id, QRCODE_QUERY, biz_key)

    def query_game_qrcode_state(self, ticket: str, app_id: int, biz_key: str = "") -> Tuple[LoginQRCodeState, str, str]:
        """
        查询游戏内二维码状态
        Returns: (state, uid, token)
        
        **注意**: 游戏内二维码 (app_id!=1) 的 query 端点不工作（返回 -105）。
        C++ 中 GetQRCodeState 仅用于自生成二维码 (app_id=1) 的 fetch→query 轮询。
        游戏内二维码使用 scan→confirm 流程，无轮询。
        
        API 地址:
        - app_id=1  (崩坏3): BH3_QRCODE_QUERY
        - app_id=4  (原神): HK4E_QRCODE_QUERY (api-sdk, 但返回-105)
        - app_id=8  (星铁): HKRPG_QRCODE_QUERY
        - app_id=12 (绝区零): NAP_QRCODE_QUERY
        """
        # 根据 app_id 选择正确的 API URL
        # 参考 C++ ApiDefs.hpp：query 用 hk4e-sdk 域名
        if app_id == 1:  # 崩坏3
            api_url = BH3_QRCODE_QUERY
        elif app_id == 4:  # 原神 - query 用 hk4e-sdk
            api_url = HK4E_QRCODE_QUERY
        elif app_id == 8:  # 星铁
            api_url = HKRPG_QRCODE_QUERY
        elif app_id == 12:  # 绝区零
            api_url = NAP_QRCODE_QUERY
        else:
            api_url = QRCODE_QUERY

        qr_log(f"[query_game_qrcode_state] app_id={app_id}, api_url={api_url}", LogLevel.DEBUG)
        return self._query_qrcode_state_impl(ticket, app_id, api_url, biz_key)

    def _query_qrcode_state_impl(self, ticket: str, app_id: int, api_url: str, biz_key: str = "") -> Tuple[LoginQRCodeState, str, str]:
        """
        内部实现：查询二维码状态（对齐 C++ GetQRCodeState）
        Returns: (state, uid, token)

        C++ MhyApi.hpp GetQRCodeState:
        - 请求体仅 {app_id, device, ticket}，无 biz_key
        - 请求头仅 Content-Type: application/json，无 x-rpc 系列头
        - 无状态 POST，不使用 session
        - retcode != 0 → 直接返回 Expired（无子类型区分）
        - 未知 stat → 返回 Expired
        """
        data = {
            "app_id": app_id,
            "device": self.device_id,
            "ticket": ticket
        }

        # 使用 curl_cffi 模拟 libcurl TLS 指纹，与 C++ 行为一致
        # C++ GetQRCodeState 仅发 Content-Type，不带 User-Agent / x-rpc-* 头
        headers = {"Content-Type": "application/json"}

        qr_log(f"[query_qrcode_state] URL={api_url}, ticket={ticket[:12]}..., app_id={app_id}", LogLevel.DEBUG)

        try:
            resp = _safe_post(api_url, json_data=data, headers=headers)
            result = resp.json()
            qr_log(f"[query_qrcode_state] response: {result}", LogLevel.DEBUG)

            retcode = result.get("retcode", -1)
            # 对齐 C++ MhyApi.hpp: if (retcode != 0) return Expired
            # C++ 把任何非0 retcode（包括 -3503 WAF风控）都当作 Expired，
            # 这样会触发二维码自动刷新，重新获取 ticket 后不再被 WAF 拦截。
            # Python 之前把 -3503 当作 Init 继续轮询 → 同一个 ticket 被 WAF 永久封堵 → 死循环。
            if retcode != 0:
                msg = result.get("message", "")
                qr_log(f"[query_qrcode_state] retcode={retcode} msg={msg} → Expired", LogLevel.WARN)
                return LoginQRCodeState.Expired, "", ""

            # data 可能为 None
            if not result.get("data"):
                qr_log(f"[query_qrcode_state] data is None → Init", LogLevel.DEBUG)
                return LoginQRCodeState.Init, "", ""

            stat = result["data"]["stat"]
            qr_log(f"[query_qrcode_state] stat='{stat}'", LogLevel.DEBUG)

            state_map = {
                "Init": LoginQRCodeState.Init,
                "Scanned": LoginQRCodeState.Scanned,
                "Confirmed": LoginQRCodeState.Confirmed,
            }

            state = state_map.get(stat, LoginQRCodeState.Expired)

            # Confirmed 状态解析 payload 获取 uid/token
            if state == LoginQRCodeState.Confirmed:
                try:
                    payload = json.loads(result["data"]["payload"]["raw"])
                    uid = payload.get("uid", "")
                    token = payload.get("token", "")
                    qr_log(f"[query_qrcode_state] Confirmed! uid={uid}")
                    return state, uid, token
                except Exception as e:
                    qr_log(f"[query_qrcode_state] Confirmed payload解析失败: {e}", LogLevel.ERROR)
                    return LoginQRCodeState.Expired, "", ""

            qr_log(f"[query_qrcode_state] returning state={state.name}", LogLevel.DEBUG)
            return state, "", ""
        except Exception as e:
            qr_log(f"查询二维码状态异常: {e}", LogLevel.WARN)
            # 异常时不返回 Expired，返回 Init 保持轮询（避免网络抖动导致误判）
            return LoginQRCodeState.Init, "", ""
    
    def scan_qrcode(self, ticket: str, app_id: int = 1, biz_key: str = "", url: str = "") -> bool:
        """
        扫码确认（参考 C++ ScanQRLogin）
        
        C++ 中 ScanQRLogin 使用 ApiDefs.hpp 中定义的固定 API URL（api-sdk 域名）。
        C++ 请求体仅含 app_id, device, ticket（无 biz_key）。
        """
        # 如果传入了自定义 URL，优先使用
        if url:
            api_url = url
        else:
            # 根据 app_id 选择正确的 API URL
            if app_id == 1:  # 自生成二维码（与 fetch/query 同一 hk4e-sdk 域名）
                api_url = QRCODE_SCAN
            elif app_id == 4:  # 原神 - 游戏内二维码使用 api-sdk 域名
                api_url = HK4E_QRCODE_SCAN
            elif app_id == 8:  # 星铁
                api_url = HKRPG_QRCODE_SCAN
            elif app_id == 12:  # 绝区零
                api_url = NAP_QRCODE_SCAN
            else:
                api_url = QRCODE_SCAN

        data = {
            "app_id": app_id,
            "device": self.device_id,
            "ticket": ticket
        }
        if biz_key:
            data["biz_key"] = biz_key

        # C++ ScanQRLogin 仅发 Content-Type: application/json，无 User-Agent / x-rpc-* 头
        headers = {"Content-Type": "application/json"}

        try:
            qr_log(f"[scan_qrcode] 发送请求到: {api_url}", LogLevel.DEBUG)
            qr_log(f"[scan_qrcode] 请求数据: {data}", LogLevel.DEBUG)
            # 无状态 POST（C++ 中 scan 不使用 session）
            resp = _safe_post(api_url, json_data=data, headers=headers)
            result = resp.json()
            qr_log(f"[scan_qrcode] response: {result}", LogLevel.DEBUG)
            return result.get("retcode", -1) == 0
        except Exception as e:
            qr_log(f"扫码确认异常: {e}", LogLevel.ERROR)
            return False
    
    def confirm_qrcode(self, ticket: str, uid: str, token: str, app_id: int = 1, biz_key: str = "", url: str = "") -> bool:
        """
        确认登录（对齐 C++ ConfirmQRLogin / scanConfirm）

        对齐 DSVVA C++ ConfirmQRLogin:
        - 非 BH3 游戏: POST 到 url 参数指定的地址（若 url 为空则自动选择 API 端点）
        - BH3 Bilibili 服 (server_type=BiliBili): 使用 scanConfirm 流程（Combo proto + OA）
        - 请求体包含 Account proto 嵌套 payload: {uid, token}
        """
        game_type = app_id  # 对齐 C++: static_cast<int>(gameType)

        # ── BH3 Bilibili 服特殊流程 ──
        if game_type == int(GameType.Honkai3) and self.server_type == ServerType.BiliBili:
            # 传入的 token 在 BH3 B服场景下是 access_key
            return self._scan_confirm_bh3(ticket, uid, token, "")

        # ── URL 直发模式（对齐 DSVVA ConfirmQRLogin POST 到二维码 URL） ──
        if url:
            return self._confirm_qrcode_by_url(url, ticket, uid, token, game_type)

        # ── 自动选择 API 端点 ──
        if app_id == 1:  # 崩坏3
            api_url = BH3_QRCODE_CONFIRM
        elif app_id == 4:  # 原神 - 游戏内二维码使用 api-sdk 域名
            api_url = HK4E_QRCODE_CONFIRM
        elif app_id == 8:  # 星铁
            api_url = HKRPG_QRCODE_CONFIRM
        elif app_id == 12:  # 绝区零
            api_url = NAP_QRCODE_CONFIRM
        else:
            api_url = QRCODE_CONFIRM
        
        data = {
            "app_id": app_id,
            "device": self.device_id,
            "ticket": ticket,
            "payload": {
                "proto": "Account",
                "raw": json.dumps({"uid": uid, "token": token})
            }
        }
        if biz_key:
            data["biz_key"] = biz_key

        # C++ ConfirmQRLogin 仅发 Content-Type: application/json，无 User-Agent / x-rpc-* 头
        headers = {"Content-Type": "application/json"}

        qr_log(f"[confirm_qrcode] 请求数据: {data}", LogLevel.DEBUG)
        qr_log(f"[confirm_qrcode] 请求URL: {api_url}", LogLevel.DEBUG)

        try:
            # 无状态 POST（与 C++ 一致）
            resp = _safe_post(api_url, json_data=data, headers=headers)
            result = resp.json()
            qr_log(f"[confirm_qrcode] 响应: {result}", LogLevel.DEBUG)
            return result.get("retcode", -1) == 0
        except Exception as e:
            qr_log(f"确认登录异常: {e}", LogLevel.ERROR)
            return False
    
    def _confirm_qrcode_by_url(self, url: str, ticket: str, uid: str, game_token: str, app_id: int) -> bool:
        """
        URL 直发确认登录（对齐 DSVVA C++ ConfirmQRLogin）

        与 confirm_qrcode 的区别：
        - 直接 POST 到二维码 URL 本身（而非 API 端点）
        - DSVVA 从屏幕截取二维码后，提取 URL 并用此方法确认
        - 请求体与 confirm_qrcode 相同（Account proto）
        """
        data = {
            "app_id": app_id,
            "device": self.device_id,
            "ticket": ticket,
            "payload": {
                "proto": "Account",
                "raw": json.dumps({"uid": uid, "token": game_token})
            }
        }
        headers = {"Content-Type": "application/json"}

        qr_log(f"[confirm_by_url] URL={url[:80]}...", LogLevel.DEBUG)
        qr_log(f"[confirm_by_url] data={data}", LogLevel.DEBUG)

        try:
            resp = _safe_post(url, json_data=data, headers=headers)
            result = resp.json()
            qr_log(f"[confirm_by_url] response: {result}", LogLevel.DEBUG)
            return result.get("retcode", -1) == 0
        except Exception as e:
            qr_log(f"[confirm_by_url] 异常: {e}", LogLevel.ERROR)
            return False

    def _get_bh3_oa_string(self) -> str:
        """
        获取崩坏3 Bilibili 服 OA 调度信息（对齐 C++ GetOAString）

        C++ 实现:
          static std::string value = []() {
              auto res = cpr::Get(cpr::Url{"https://api.v6qbb.cloud/get_bh3_bilibili_oa"});
              return res.text;
          }();
        """
        try:
            import requests as req
            resp = req.get(BH3_BILIBILI_OA_URL, timeout=10)
            if resp.text.strip():
                qr_log(f"[bh3_oa] 获取成功: {resp.text[:80]}...", LogLevel.DEBUG)
                return resp.text.strip()
        except Exception as e:
            qr_log(f"[bh3_oa] 获取失败: {e}", LogLevel.WARN)
        return ""

    def get_bh3_external_login_info(self, uid: str, access_key: str) -> Tuple[int, str, str, str]:
        """
        获取崩坏3 Bilibili 服外部登录信息（对齐 C++ GetBH3ExternalLoginInfo）

        POST https://api-sdk.mihoyo.com/bh3_cn/combo/granter/login/v2/login
        Returns: (retcode, open_id, combo_token, combo_id)
        """
        import requests as req

        body_data = json.dumps({"access_key": access_key, "uid": int(uid)})
        body = {
            "device": "0000000000000000",
            "app_id": 1,
            "channel_id": 14,
            "data": body_data
        }
        # C++ 会调用 makeSign(body) 生成 HMAC-SHA256 签名
        # 对齐 C++ 的固定密钥
        body["sign"] = hmac_sha256(
            "0ebc517adb1b62c6b408df153331f9aa",
            body_data
        )

        headers = {"Content-Type": "application/json"}

        qr_log(f"[bh3_external_login] uid={uid}", LogLevel.DEBUG)
        qr_log(f"[bh3_external_login] body={body}", LogLevel.DEBUG)

        try:
            resp = req.post(BH3_V2_LOGIN, json=body, headers=headers, timeout=10)
            result = resp.json()
            qr_log(f"[bh3_external_login] response: {result}", LogLevel.DEBUG)

            retcode = result.get("retcode", -1)
            if retcode != 0:
                return retcode, "", "", ""

            open_id = result["data"]["open_id"]
            combo_token = result["data"]["combo_token"]
            combo_id = result["data"]["combo_id"]
            return 0, open_id, combo_token, combo_id
        except Exception as e:
            qr_log(f"[bh3_external_login] 异常: {e}", LogLevel.ERROR)
            return -1, "", "", ""

    def _bh3_scan_check(self, ticket: str) -> bool:
        """
        崩坏3 扫码校验（对齐 C++ scanCheck）

        POST https://api-sdk.mihoyo.com/bh3_cn/combo/panda/qrcode/scan
        """
        import requests as req

        data = {
            "app_id": "1",
            "device": "0000000000000000",
            "ticket": ticket,
            "ts": int(time.time())
        }

        headers = {"Content-Type": "application/json"}
        qr_log(f"[bh3_scan_check] ticket={ticket[:12]}...", LogLevel.DEBUG)

        try:
            resp = req.post(BH3_QRCODE_SCAN, json=data, headers=headers, timeout=10)
            result = resp.json()
            qr_log(f"[bh3_scan_check] response: {result}", LogLevel.DEBUG)
            return result.get("retcode", -1) == 0
        except Exception as e:
            qr_log(f"[bh3_scan_check] 异常: {e}", LogLevel.ERROR)
            return False

    def _scan_confirm_bh3(self, ticket: str, uid: str, access_key: str, name: str = "") -> bool:
        """
        崩坏3 Bilibili 服扫码确认登录（对齐 C++ scanConfirm）

        流程:
        1. GetBH3ExternalLoginInfo(uid, access_key) → open_id, combo_token, combo_id
        2. 构建 Combo proto 的 raw + ext payload
        3. POST BH3_QRCODE_CONFIRM

        C++ scanConfirm 使用 Combo proto（而非 Account proto）
        """
        retcode, open_id, combo_token, combo_id = self.get_bh3_external_login_info(uid, access_key)
        if retcode != 0:
            qr_log(f"[bh3_scan_confirm] GetBH3ExternalLoginInfo 失败 retcode={retcode}", LogLevel.WARN)
            return False

        # 构建 raw payload（Combo proto）
        raw_data = {
            "heartbeat": False,
            "open_id": open_id,
            "device_id": "0000000000000000",
            "app_id": "1",
            "channel_id": "14",
            "combo_token": combo_token,
            "asterisk_name": name,
            "combo_id": combo_id,
            "account_type": "2"
        }

        # 获取 OA 调度字符串
        oa_string = self._get_bh3_oa_string()

        # 构建 ext payload
        ext_data = {
            "data": {
                "accountType": "2",
                "accountID": "",
                "c": open_id,
                "accountToken": combo_token,
                "dispatch": oa_string
            }
        }

        post_body = {
            "device": "0000000000000000",
            "app_id": 1,
            "ts": int(time.time()),
            "ticket": ticket,
            "payload": {
                "proto": "Combo",
                "raw": json.dumps(raw_data),
                "ext": json.dumps(ext_data)
            }
        }

        headers = {"Content-Type": "application/json"}
        qr_log(f"[bh3_scan_confirm] post_body={json.dumps(post_body, ensure_ascii=False)[:300]}...", LogLevel.DEBUG)

        try:
            import requests as req
            resp = req.post(BH3_QRCODE_CONFIRM, json=post_body, headers=headers, timeout=10)
            result = resp.json()
            qr_log(f"[bh3_scan_confirm] response: {result}", LogLevel.DEBUG)
            return result.get("retcode", -1) == 0
        except Exception as e:
            qr_log(f"[bh3_scan_confirm] 异常: {e}", LogLevel.ERROR)
            return False

    # ═══════════════════════════════════════════════════════════════
    # PandaScan + passport 扫码登录（对齐 C++ MhyApi.hpp）
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def get_qrcode_scan_url(app_id: int) -> str:
        """根据 app_id 返回游戏内二维码 scan 端点（对齐 C++ setGameTypeByBizKey / setGameTypeByAppId）"""
        if app_id == 1:
            return BH3_QRCODE_SCAN
        elif app_id == 4:
            return HK4E_QRCODE_SCAN
        elif app_id == 8:
            return HKRPG_QRCODE_SCAN
        elif app_id == 12:
            return NAP_QRCODE_SCAN
        return HK4E_QRCODE_SCAN

    @staticmethod
    def get_passport_qr_param(qr_code: str, key: str, terminators: str = "&") -> str:
        """从 passport 二维码 URL 中提取参数（对齐 C++ getPassportQRParam）"""
        needle = key + "="
        begin = qr_code.find(needle)
        if begin == -1:
            return ""
        value_begin = begin + len(needle)
        value_end = len(qr_code)
        for t in terminators:
            idx = qr_code.find(t, value_begin)
            if idx != -1:
                value_end = idx
                break
        return qr_code[value_begin:value_end]

    def panda_scan_qrcode(self, scan_url: str, ticket: str, app_id: int) -> str:
        """
        Panda 扫码（对齐 C++ PandaScanQRCode）

        向游戏内二维码对应的 scan 端点发送扫码请求，返回 passport 二维码 URL
        （passport_qr_url），后续用 ScanPassportQRLogin / ConfirmPassportQRLogin 处理。
        请求体与 C++ 完全一致：passport_app_id + ticket + app_id + device + ts
        """
        if not scan_url or not ticket:
            qr_log("[panda_scan] 缺少 scan_url 或 ticket", LogLevel.WARN)
            return ""

        data = {
            "passport_app_id": PASSPORT_PANDA_APP_ID,
            "ticket": ticket,
            "app_id": int(app_id),
            "device": self.device_id,
            "ts": int(time.time()),
        }
        # 对齐 C++ PandaScanQRCode：必须带 x-rpc-app_id 与 x-rpc-device_id 头
        headers = {
            "Content-Type": "application/json",
            "x-rpc-app_id": PASSPORT_PANDA_APP_ID,
            "x-rpc-device_id": self.device_id,
        }

        try:
            qr_log(f"[panda_scan] POST {scan_url}", LogLevel.DEBUG)
            resp = _safe_post(scan_url, json_data=data, headers=headers)
            result = resp.json()
            qr_log(f"[panda_scan] response: {result}", LogLevel.DEBUG)

            if result.get("retcode", -1) != 0:
                qr_log(f"[panda_scan] retcode={result.get('retcode')} msg={result.get('message')}", LogLevel.WARN)
                return ""

            passport_url = result.get("data", {}).get("passport_qr_url", "")
            qr_log(f"[panda_scan] passport_qr_url={'有' if passport_url else '无'}", LogLevel.DEBUG)
            return passport_url
        except Exception as e:
            qr_log(f"[panda_scan] 异常: {e}", LogLevel.ERROR)
            return ""

    def _passport_qr_login(self, passport_qr_url: str, stoken: str, mid: str, confirm: bool) -> bool:
        """
        passport 扫码/确认（对齐 C++ PassportQRCodeLogin）

        ticket = passport 二维码中的 tk 参数
        token_types = passport 二维码中的 token_types 参数（以 # 结尾）
        POST passport-api.mihoyo.com/.../app/{scan|confirm}QRLogin，带 stoken;mid Cookie
        """
        ticket = self.get_passport_qr_param(passport_qr_url, "tk", "&")
        token_types = self.get_passport_qr_param(passport_qr_url, "token_types", "#")
        if not ticket or not token_types:
            qr_log("[passport_qr] 缺少 tk 或 token_types 参数", LogLevel.WARN)
            return False

        cookie = f"stoken={stoken};mid={mid}"
        body = {
            "ticket": ticket,
            "token_types": [token_types],
        }
        url = PASSPORT_CONFIRM_QR if confirm else PASSPORT_SCAN_QR
        headers = {
            "Content-Type": "application/json",
            "x-rpc-app_id": PASSPORT_PANDA_APP_ID,
            "x-rpc-device_id": self.device_id,
            "Cookie": cookie,
        }

        phase = "confirm" if confirm else "scan"
        try:
            resp = _safe_post(url, json_data=body, headers=headers)
            result = resp.json()
            qr_log(f"[passport_qr_{phase}] response: {result}", LogLevel.DEBUG)
            return result.get("retcode", -1) == 0
        except Exception as e:
            qr_log(f"[passport_qr_{phase}] 异常: {e}", LogLevel.ERROR)
            return False

    def scan_passport_qr_login(self, passport_qr_url: str, stoken: str, mid: str) -> bool:
        """passport 扫码（对齐 C++ ScanPassportQRLogin）"""
        return self._passport_qr_login(passport_qr_url, stoken, mid, False)

    def confirm_passport_qr_login(self, passport_qr_url: str, stoken: str, mid: str) -> bool:
        """passport 确认登录（对齐 C++ ConfirmPassportQRLogin）"""
        return self._passport_qr_login(passport_qr_url, stoken, mid, True)

    def check_stoken_valid(self, stoken: str, mid: str) -> bool:
        """
        校验 stoken 是否有效（对齐 C++ CheckStokenValid）

        C++ 在开扫前(pBtstartScreen)就会调用此检查，stoken 失效则直接报
        "登录状态失效，请重新添加账号！" 并中止，根本不会发起扫码请求。
        GET api-takumi.mihoyo.com/auth/api/getCookieAccountInfoBySToken
        Cookie: stoken=...;mid=...  返回 retcode==0 视为有效。
        """
        if not stoken or not mid:
            return False
        cookie = f"stoken={stoken};mid={mid}"
        headers = {"Accept": "application/json", "Cookie": cookie}
        try:
            resp = _safe_get(COOKIE_ACCOUNT_INFO, headers=headers)
            j = resp.json()
            qr_log(f"[check_stoken] response: {j}", LogLevel.DEBUG)
            return j.get("retcode", -1) == 0
        except Exception as e:
            qr_log(f"[check_stoken] 异常: {e}", LogLevel.ERROR)
            return False

    # ═══════════════════════════════════════════════════
    # hoyolab / 米游社 扫码登录（新 passport API）
    # ═══════════════════════════════════════════════════
    # 与旧 hk4e-sdk API 的核心区别：
    #   1. 端点变为 passport-api.miyoushe.com
    #   2. 状态名: Created (未扫) / Scanned (已扫) / Confirmed (确认)
    #   3. Confirmed 时 token 通过响应 Set-Cookie 头直接返回 (ltoken_v2/cookie_token)
    #   4. 无需额外 login_ticket → stoken 转换步骤
    
    def _hoyolab_headers(self) -> Dict[str, str]:
        """hoyolab QR 登录专用请求头"""
        return {
            "Content-Type": "application/json",
            "x-rpc-app_id": HOYOLAB_APP_ID,
            "x-rpc-device_id": self.device_id,
        }
    
    def fetch_hoyolab_qrcode(self) -> Tuple[str, str]:
        """获取 hoyolab 扫码登录二维码（新 passport API）

        POST https://passport-api.miyoushe.com/account/ma-cn-passport/web/createQRLogin

        Returns: (qrcode_url, ticket)
        - qrcode_url: 用于生成二维码图片的 URL
        - ticket: UUID 格式，用于后续轮询查询状态
        """
        headers = self._hoyolab_headers()

        try:
            resp = _safe_post(HOYOLAB_QR_CREATE, json_data={}, headers=headers)
            result = resp.json()
            qr_log(f"[hoyolab_qr_create] response: {result}", LogLevel.DEBUG)

            if result.get("retcode", -1) == 0:
                url = result["data"]["url"]
                ticket = result["data"]["ticket"]
                qr_log(f"[hoyolab_qr_create] success! ticket={ticket[:16]}...", LogLevel.DEBUG)
                return url, ticket
            else:
                qr_log(f"[hoyolab_qr_create] failed retcode={result.get('retcode')}: {result.get('message')}", LogLevel.WARN)
                return "", ""
        except Exception as e:
            qr_log(f"[hoyolab_qr_create] 异常: {e}", LogLevel.ERROR)
            return "", ""
    
    def query_hoyolab_qrcode_status(self, ticket: str) -> Tuple[str, str, str, str]:
        """查询 hoyolab 扫码登录二维码状态（新 passport API）

        POST https://passport-api.miyoushe.com/account/ma-cn-passport/web/queryQRLoginStatus

        Returns: (status_name, uid, mid, token)
        - status_name: "Created" / "Scanned" / "Confirmed" / "Expired"
        - uid/mid/token: 仅 Confirmed 时有值
          - token: 从响应 Set-Cookie 提取 (优先级: ltoken_v2 > cookie_token)

        特殊 retcode:
        - -3501: 二维码已过期
        - -3505: 用户取消扫码
        """
        headers = self._hoyolab_headers()
        data = {"ticket": ticket}

        try:
            resp = _safe_post(HOYOLAB_QR_QUERY, json_data=data, headers=headers)
            result = resp.json()
            qr_log(f"[hoyolab_qr_query] response: {result}", LogLevel.DEBUG)

            retcode = result.get("retcode", -1)

            # 过期/取消
            if retcode == -3501:
                qr_log(f"[hoyolab_qr_query] 二维码已过期 (-3501)", LogLevel.DEBUG)
                return "Expired", "", "", ""
            if retcode == -3505:
                qr_log(f"[hoyolab_qr_query] 用户取消扫码 (-3505)", LogLevel.DEBUG)
                return "Expired", "", "", ""
            if retcode != 0:
                msg = result.get("message", "")
                qr_log(f"[hoyolab_qr_query] retcode={retcode} msg={msg} → Expired", LogLevel.WARN)
                return "Expired", "", "", ""

            status = result["data"]["status"]
            qr_log(f"[hoyolab_qr_query] status={status}", LogLevel.DEBUG)

            if status == "Confirmed":
                user_info = result["data"].get("user_info", {})
                uid = str(user_info.get("aid", ""))
                mid = str(user_info.get("mid", ""))

                # 从 Set-Cookie 提取 token
                token = self._extract_token_from_cookies(resp)
                qr_log(
                    f"[hoyolab_qr_query] Confirmed! uid={uid}, mid={mid}, "
                    f"token={'✓' if token else '✗'}",
                    LogLevel.DEBUG
                )
                return "Confirmed", uid, mid, token

            elif status == "Scanned":
                return "Scanned", "", "", ""
            elif status == "Created":
                return "Created", "", "", ""
            else:
                qr_log(f"[hoyolab_qr_query] unknown status={status}", LogLevel.WARN)
                return "Expired", "", "", ""

        except Exception as e:
            qr_log(f"[hoyolab_qr_query] 异常: {e}", LogLevel.WARN)
            return "Created", "", "", ""
    
    def _extract_token_from_cookies(self, resp) -> str:
        """从 HTTP 响应中提取登录 token（优先级: ltoken_v2 > cookie_token）

        hoyolab QR 确认登录后，token 通过 Set-Cookie 头返回，不在 JSON body 中。
        常见 cookie 名称: ltoken_v2, cookie_token, ltoken, stoken
        """
        try:
            # 方式1: 从响应 cookies 属性获取
            for name in ["ltoken_v2", "cookie_token", "ltoken", "stoken"]:
                val = self._get_cookie(resp, name)
                if val:
                    qr_log(f"[cookie_extract] found {name}={val[:16]}...", LogLevel.DEBUG)
                    return val

            # 方式2: 解析 Set-Cookie 原始头
            set_cookie = resp.headers.get("Set-Cookie", "")
            if set_cookie:
                import re
                for name in ["ltoken_v2", "cookie_token", "ltoken", "stoken"]:
                    match = re.search(rf'{name}=([^;]+)', set_cookie)
                    if match:
                        val = match.group(1)
                        qr_log(f"[cookie_extract] found {name} from Set-Cookie header", LogLevel.DEBUG)
                        return val

            qr_log("[cookie_extract] no token found in cookies", LogLevel.WARN)
            return ""
        except Exception as e:
            qr_log(f"[cookie_extract] error: {e}", LogLevel.WARN)
            return ""
    
    @staticmethod
    def _get_cookie(resp, name: str) -> str:
        """从响应对象提取指定名称的 cookie"""
        try:
            cookies = resp.cookies
            # requests.cookies (RequestsCookieJar) / curl_cffi cookies
            if hasattr(cookies, 'get'):
                return cookies.get(name, "")
            else:
                return cookies.get(name, "")
        except Exception:
            return ""
    
    # ═══════════════════════════════════════════════════════════════
    
    def get_stoken_by_game_token(self, uid: str, game_token: str, ticket: str = "", biz_key: str = "") -> Tuple[int, str, str]:
        """通过game_token获取stoken（已废弃，保留用于回退兼容）。

        该接口 getTokenByGameToken 已被米哈游废弃（返回 -5300）。
        请使用 get_stoken_by_login_ticket() 替代。
        """
        import requests as req

        data = {
            "account_id": int(uid),
            "game_token": game_token
        }

        headers = get_request_headers()

        api_log(f"[get_stoken_by_game_token] uid={uid}", LogLevel.DEBUG)
        api_log(f"[get_stoken_by_game_token] request: {json.dumps(data)}", LogLevel.DEBUG)

        try:
            resp = req.post(GAME_TOKEN_STOKEN, json=data, headers=headers, timeout=10)
            result = resp.json()
            api_log(f"[get_stoken_by_game_token] response: {result}", LogLevel.DEBUG)

            retcode = result.get("retcode", -1)
            if retcode == 0:
                mid = result["data"]["user_info"]["mid"]
                stoken = result["data"]["token"]["token"]
                api_log(f"[get_stoken_by_game_token] success!")
                return 0, mid, stoken
            else:
                api_log(f"[get_stoken_by_game_token] retcode={retcode}, msg={result.get('message', '')}", LogLevel.WARN)
                return retcode, "", ""
        except Exception as e:
            api_log(f"[get_stoken_by_game_token] ex: {e}", LogLevel.ERROR)
            return -1, "", ""

    def get_stoken_by_login_ticket(self, uid: str, login_ticket: str) -> Tuple[int, str, str]:
        """通过 login_ticket 获取 stoken（新版 API，替代已废弃的 getTokenByGameToken）。

        扫码确认后从 hk4e-sdk query 返回的 token 实际是 login_ticket，
        通过 getMultiTokenByLoginTicket 接口可直接兑换 stoken。

        Returns: (retcode, mid, stoken)
        """
        import requests as req

        data = {
            "login_ticket": login_ticket,
            "token_types": [3],
            "uid": uid
        }

        headers = get_request_headers()
        # x-rpc-game_biz: bbs_cn — 通行证/BBS 流程

        api_log(f"[get_stoken_by_login_ticket] uid={uid}", LogLevel.DEBUG)
        api_log(f"[get_stoken_by_login_ticket] request: {json.dumps(data)}", LogLevel.DEBUG)

        try:
            resp = req.post(MULTI_TOKEN, json=data, headers=headers, timeout=10)
            result = resp.json()
            api_log(f"[get_stoken_by_login_ticket] response: {result}", LogLevel.DEBUG)

            retcode = result.get("retcode", -1)
            if retcode == 0:
                mid = result["data"]["user_info"]["mid"]
                stoken = result["data"]["token"]["token"]
                api_log(f"[get_stoken_by_login_ticket] success!")
                return 0, mid, stoken
            else:
                api_log(f"[get_stoken_by_login_ticket] retcode={retcode}, msg={result.get('message', '')}", LogLevel.WARN)
                return retcode, "", ""
        except Exception as e:
            api_log(f"[get_stoken_by_login_ticket] ex: {e}", LogLevel.ERROR)
            return -1, "", ""
    
    def get_game_token_by_stoken(self, stoken: str, mid: str) -> Tuple[int, str]:
        """
        通过stoken获取game_token（参考 C++ GetGameTokenByStoken — 无自定义headers）
        Returns: (retcode, game_token)
        """
        params = {
            "stoken": stoken,
            "mid": mid
        }
        
        # C++ GetGameTokenByStoken 不发送任何自定义 headers，无状态 GET
        try:
            resp = requests.get(GAME_TOKEN, params=params, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            
            if result.get("retcode", -1) == 0:
                return 0, result["data"]["game_token"]
            else:
                return result.get("retcode", -1), ""
        except Exception as e:
            api_log(f"获取game_token异常: {e}", LogLevel.WARN)
            return -1, ""
    
    def get_mys_user_name(self, uid: str) -> str:
        """获取米游社用户名（对齐 C++ getMysUserName — 无自定义headers，无状态GET）"""
        import requests as req

        url = f"{USER_INFO}?uid={uid}"

        # C++ getMysUserName: cpr::Get + {url}?uid={uid}，无任何自定义 headers
        try:
            resp = req.get(url, timeout=10)
            result = resp.json()
            qr_log(f"[get_mys_user_name] response: {result}", LogLevel.DEBUG)

            if result.get("retcode", 0) == 0:
                return result["data"]["user_info"]["nickname"]
            else:
                return ""
        except Exception as e:
            qr_log(f"获取用户名异常: {e}", LogLevel.WARN)
            return ""
    
    # ========== 短信登录 ==========
    
    def send_sms_code(self, phone: str) -> Tuple[int, str, Dict[str, Any]]:
        """
        发送短信验证码
        Args:
            phone: 手机号 (如 13800138000 或 +8613800138000)
        Returns: (retcode, action_type, extra_data)
        """
        global _last_sms_send_time
        
        current_time = time.time()
        if current_time - _last_sms_send_time < SMS_COOLDOWN_SECONDS:
            remaining = int(SMS_COOLDOWN_SECONDS - (current_time - _last_sms_send_time))
            api_log(f"[send_sms_code] 发送过于频繁，请等待 {remaining} 秒后再试", LogLevel.WARN)
            return -3002, f"发送过于频繁，请等待 {remaining} 秒后再试", {}
        _last_sms_send_time = current_time
        
        import requests as req
        
        # 提取纯手机号（去掉+86前缀）
        clean_phone = phone
        if clean_phone.startswith("+86"):
            clean_phone = clean_phone[3:]
        elif clean_phone.startswith("86"):
            clean_phone = clean_phone[2:]
        
        # RSA加密手机号和区号（新版API两者都需要加密）
        encrypted_phone = self._rsa_encrypt(clean_phone)
        encrypted_area_code = self._rsa_encrypt("+86")
        
        body_data = {
            "area_code": encrypted_area_code,
            "mobile": encrypted_phone
        }
        body_str = json.dumps(body_data, separators=(',', ':'))
        
        # 使用游戏客户端风格请求头（无DS签名）
        lifecycle_id = str(uuid.uuid4())
        headers = get_sms_request_headers(lifecycle_id)
        headers["Content-Length"] = str(len(body_str))
        
        api_log(f"[send_sms_code] 发送请求到: {SMS_CREATE}", LogLevel.DEBUG)
        api_log(f"[send_sms_code] 请求体: {body_str}", LogLevel.DEBUG)
        
        try:
            resp = req.post(SMS_CREATE, data=body_str.encode(), headers=headers, timeout=10)
            result = resp.json()
            api_log(f"[send_sms_code] response: {result}", LogLevel.DEBUG)
            
            retcode = result.get("retcode", -1)
            
            if retcode == 0:
                action_type = result.get("data", {}).get("action_type", "")
                return 0, action_type, {}
            else:
                return retcode, result.get("message", ""), {}
        except Exception as e:
            api_log(f"[send_sms_code] exception: {e}", LogLevel.ERROR)
            return -1, str(e), {}
    
    def login_by_sms(self, phone: str, captcha: str, action_type: str = "") -> Tuple[int, str, str, str]:
        """
        短信验证码登录
        Returns: (retcode, v2_token, uid, mid)
        """
        import requests as req
        
        # 提取纯手机号
        clean_phone = phone
        if clean_phone.startswith("+86"):
            clean_phone = clean_phone[3:]
        elif clean_phone.startswith("86"):
            clean_phone = clean_phone[2:]
        
        # RSA加密手机号和区号（新版API两者都需要加密）
        encrypted_phone = self._rsa_encrypt(clean_phone)
        encrypted_area_code = self._rsa_encrypt("+86")
        
        body_data = {
            "area_code": encrypted_area_code,
            "action_type": action_type,
            "captcha": captcha,
            "mobile": encrypted_phone
        }
        body_str = json.dumps(body_data, separators=(',', ':'))
        
        # 使用游戏客户端风格请求头（无DS签名）
        lifecycle_id = str(uuid.uuid4())
        headers = get_sms_request_headers(lifecycle_id)
        headers["Content-Length"] = str(len(body_str))
        
        api_log(f"[login_by_sms] 发送请求到: {SMS_LOGIN}", LogLevel.DEBUG)
        api_log(f"[login_by_sms] 请求体: {body_str}", LogLevel.DEBUG)
        
        try:
            resp = req.post(SMS_LOGIN, data=body_str.encode(), headers=headers, timeout=10)
            result = resp.json()
            api_log(f"[login_by_sms] response: {result}", LogLevel.DEBUG)
            
            retcode = result.get("retcode", -1)
            
            if retcode == 0:
                v2_token = result["data"]["token"]["token"]
                uid = str(result["data"]["user_info"]["aid"])
                mid = result["data"]["user_info"]["mid"]
                return 0, v2_token, uid, mid
            elif retcode == -3205:
                return -3205, "", "", ""
            else:
                return retcode, "", "", ""
        except Exception as e:
            api_log(f"[login_by_sms] exception: {e}", LogLevel.ERROR)
            return -1, "", "", ""
    
    def _rsa_encrypt(self, data: str) -> str:
        """RSA加密（复用模块级函数）"""
        return rsa_encrypt(data)
    
    # ========== 崩坏3 BiliBili 专用 ==========
    
    def bh3_external_login(self, uid: str, access_key: str) -> Tuple[int, str, str, str]:
        """
        崩坏3 BiliBili 外部登录
        Returns: (retcode, open_id, combo_token, combo_id)
        """
        body_data = json.dumps({
            "access_key": access_key,
            "uid": int(uid)
        })
        
        body = {
            "device": "0000000000000000",
            "app_id": 1,
            "channel_id": 14,
            "data": body_data
        }
        
        # 签名
        param_str = "&".join([f"{k}={v}" for k, v in body.items() if k != "sign"])
        sign_key = "0ebc517adb1b62c6b408df153331f9aa"
        body["sign"] = hmac_sha256(sign_key, param_str)
        
        headers = {
            "Content-Type": "application/json"
        }
        
        try:
            resp = requests.post(BH3_V2_LOGIN, json=body, headers=headers, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            
            if result.get("retcode", -1) == 0:
                return (
                    0,
                    result["data"]["open_id"],
                    result["data"]["combo_token"],
                    result["data"]["combo_id"]
                )
            else:
                return result.get("retcode", -1), "", "", ""
        except Exception as e:
            api_log(f"BH3外部登录异常: {e}", LogLevel.ERROR)
            return -1, "", "", ""
    
    def bh3_qrcode_scan(self, ticket: str) -> bool:
        """崩坏3 扫码"""
        body = {
            "app_id": "1",
            "device": "0000000000000000",
            "ticket": ticket,
            "ts": int(time.time())
        }
        
        headers = {
            "Content-Type": "application/json"
        }
        
        try:
            resp = requests.post(BH3_QRCODE_SCAN, json=body, headers=headers, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            return result.get("retcode", -1) == 0
        except Exception as e:
            qr_log(f"BH3扫码异常: {e}", LogLevel.ERROR)
            return False
    
    def bh3_qrcode_confirm(self, ticket: str, uid: str, access_key: str, name: str) -> ScanRet:
        """崩坏3 确认登录"""
        code, open_id, combo_token, combo_id = self.bh3_external_login(uid, access_key)
        
        if code != 0:
            return ScanRet.FAILURE_2
        
        raw = {
            "heartbeat": False,
            "open_id": open_id,
            "device_id": "0000000000000000",
            "app_id": "1",
            "channel_id": "14",
            "combo_token": combo_token,
            "asterisk_name": name,
            "combo_id": combo_id,
            "account_type": "2"
        }
        
        ext = {
            "data": {
                "accountType": "2",
                "accountID": "",
                "c": open_id,
                "accountToken": combo_token,
                "dispatch": "https://sg-hs4-api.hovipatch.net"
            }
        }
        
        post_body = {
            "device": "0000000000000000",
            "app_id": 1,
            "ts": int(time.time()),
            "ticket": ticket,
            "payload": {
                "proto": "Combo",
                "raw": json.dumps(raw),
                "ext": json.dumps(ext)
            }
        }
        
        headers = {
            "Content-Type": "application/json"
        }
        
        try:
            resp = requests.post(BH3_QRCODE_CONFIRM, json=post_body, headers=headers, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            
            if result.get("retcode", -1) == 0:
                return ScanRet.SUCCESS
            else:
                return ScanRet.FAILURE_2
        except Exception as e:
            qr_log(f"BH3确认登录异常: {e}", LogLevel.ERROR)
            return ScanRet.FAILURE_2
    
    def parse_qrcode_url(self, url: str) -> Tuple[str, str]:
        """
        解析二维码URL，提取ticket和game_type
        Returns: (ticket, game_type_prefix)
        """
        try:
            # URL格式: https://hk4e-sdk.mihoyo.com/...ticket=xxx...
            if "ticket=" in url:
                ticket = url.split("ticket=")[-1][:24]  # 取ticket
                
                # 判断游戏类型
                if "hkrpg" in url or "SR" in url:
                    return ticket, "hkrpg"
                elif "zzz" in url or "PJSH" in url:
                    return ticket, "zzz"
                elif "bh3" in url:
                    return ticket, "bh3"
                elif "genshin" in url or "hk4e" in url:
                    return ticket, "genshin"
                
                return ticket, ""
            return "", ""
        except Exception as e:
            api_log(f"解析二维码URL异常: {e}", LogLevel.WARN)
            return "", ""


def get_game_type_from_url(url: str) -> Tuple[GameType, int]:
    """
    根据URL offset 79处的3个字符判断游戏类型
    C++项目逻辑:
      "8F3" -> 崩坏3   (app_id=1)
      "9E&" -> 原神     (app_id=4)
      "8F%" -> 星穹铁道 (app_id=8)
      "%BA" -> 绝区零   (app_id=12)
    Returns: (GameType, app_id)
    """
    if len(url) < 82:
        return GameType.UNKNOW, 0

    # URL offset 79 处取 3 个字符
    offset_79 = url[79:82]

    game_mappings = {
        "8F3": (GameType.Honkai3, 1),           # 崩坏3
        "9E&": (GameType.Genshin, 4),           # 原神
        "8F%": (GameType.HonkaiStarRail, 8),   # 星穹铁道
        "%BA": (GameType.ZenlessZoneZero, 12), # 绝区零
    }

    if offset_79 in game_mappings:
        return game_mappings[offset_79]

    # 回退：根据URL关键词判断
    if "hkrpg" in url or "SR" in url or "StarRail" in url:
        return GameType.HonkaiStarRail, 8
    elif "zzz" in url or "nap" in url.lower() or "Zenless" in url:
        return GameType.ZenlessZoneZero, 12
    elif "bh3" in url.lower() or "honkai3" in url.lower():
        return GameType.Honkai3, 1
    elif "genshin" in url or "hk4e" in url:
        return GameType.Genshin, 4

    return GameType.UNKNOW, 0
