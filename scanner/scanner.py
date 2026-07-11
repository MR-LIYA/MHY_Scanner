"""
二维码扫描模块
支持屏幕扫描和直播流扫描
"""
import os
import time
import shutil
import subprocess
import traceback
import queue
import threading
import cv2
import numpy as np
from typing import Optional, Callable, Tuple
from PyQt6.QtCore import QThread, pyqtSignal
import mss
from api import GameType, ServerType, get_game_type_from_url
from core.logger import scanner_log, info, debug, error, LogLevel

# 调试开关：是否保存最新截图
DEBUG_SAVE_SCREENSHOT = False

# 优先使用 pyzbar（检测能力更强），降级使用 OpenCV
try:
    from pyzbar.pyzbar import decode as qr_decode
    from pyzbar.pyzbar import ZBarSymbol
    USE_PYZBAR = True
    info("[ScreenScanner] 使用 pyzbar 进行二维码检测")
except ImportError:
    USE_PYZBAR = False
    info("[ScreenScanner] pyzbar 未安装，使用 OpenCV 进行二维码检测")


class ScreenScanner(QThread):
    """
    屏幕二维码扫描器
    持续监控屏幕，检测并解析二维码
    """
    qrcode_detected = pyqtSignal(str)            # 检测到二维码URL
    qrcode_game_detected = pyqtSignal(str, int, int)  # (url, game_type_val, app_id) — C++ 风格验证通过
    scan_finished = pyqtSignal(bool)              # 扫描完成
    scan_error = pyqtSignal(str)                  # 发生错误（对齐 C++ 信号名）
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = False
        self._last_ticket = ""       # C++ 风格: lastTicket，防重复
        self._used_tickets = set()   # 辅助防重复

        # OpenCV 二维码检测器（备用）
        self.detector = cv2.QRCodeDetector()

        # 调试：截图计数器（用于定期刷新）
        self._debug_counter = 0
    
    def stop(self):
        """停止扫描"""
        self._running = False
    
    def run(self):
        """开始扫描"""
        self._running = True
        self._last_ticket = ""  # 重置上次ticket
        scanner_log("扫描线程已启动")

        with mss.mss() as sct:
            monitor = sct.monitors[1]
            frame_count = 0
            last_debug_print = 0
            screenshot_ok = False

            while self._running:
                try:
                    # 截取屏幕
                    screenshot = sct.grab(monitor)

                    # 转换为OpenCV格式并缩放到1280x720（与C++项目一致）
                    img = np.array(screenshot)
                    img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                    h, w = img.shape[:2]

                    # 调试：保存最新截图（每5帧覆盖更新，加速调试）
                    if DEBUG_SAVE_SCREENSHOT:
                        self._debug_counter += 1
                        if self._debug_counter % 5 == 0:
                            screenshot_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshot")
                            os.makedirs(screenshot_dir, exist_ok=True)
                            debug_path = os.path.join(screenshot_dir, "debug_screenshot.png")
                            
                            success = cv2.imwrite(debug_path, img)
                            scanner_log(f"调试截图已保存: {debug_path} (success={success})", LogLevel.DEBUG)

                    if not screenshot_ok:
                        scanner_log(f"截图正常 (图像={w}x{h}, shape={img.shape})")
                        screenshot_ok = True
                    
                    scale_w = 1280 / w if w > 1280 else 1.0
                    scale_h = 720 / h if h > 720 else 1.0
                    scale = min(scale_w, scale_h)
                    if scale != 1.0:
                        img = cv2.resize(img, (int(w * scale), int(h * scale)))

                    # 检测并解码二维码
                    qr_data = self._detect_qr(img)
                    frame_count += 1

                    # 每30帧打印一次状态，帮助诊断
                    if frame_count - last_debug_print >= 30:
                        qr_status = "已检测" if qr_data else "无"
                        scanner_log(f"扫描中... (帧数={frame_count}, 图像={w}x{h}, 二维码={qr_status})", LogLevel.DEBUG)
                        last_debug_print = frame_count

                    # C++ 风格二维码验证：URL长度 >= 85 且 offset 79 匹配已知游戏
                    if qr_data and len(qr_data) >= 85:
                        # C++ 风格: URL offset 79 处取 3 字符判断游戏类型
                        game_type, app_id = get_game_type_from_url(qr_data)
                        if game_type == GameType.UNKNOW:
                            continue

                        # C++ 风格: ticket = URL 最后24个字符
                        ticket = qr_data[-24:]

                        # C++ 风格: 防重复 lastTicket
                        if ticket and ticket == self._last_ticket:
                            continue
                        self._last_ticket = ticket

                        scanner_log(f"检测到游戏二维码 (帧数={frame_count}, 类型={game_type.name}, app_id={app_id})")
                        self.qrcode_game_detected.emit(qr_data, int(game_type), app_id)
                        self.qrcode_detected.emit(qr_data)   # 兼容旧信号

                except Exception as e:
                    error(f"截图异常: {e}\n{traceback.format_exc()}")
                    self.scan_error.emit(str(e))

        scanner_log("扫描线程已退出")
        self.scan_finished.emit(True)
    
    def reset_last_ticket(self):
        """重置上一个ticket，允许重复检测"""
        self._last_ticket = ""

    def clear_used_tickets(self):
        """清除已使用的ticket记录"""
        self._used_tickets.clear()
        self._last_ticket = ""

    def _extract_ticket(self, qr_data: str) -> str:
        """从二维码URL中提取ticket（C++ 风格：最后24字符，兼容 ticket= 参数）"""
        # C++ 风格: 直接取最后24个字符（参考 QRCodeForScreen.cpp: str.data() + str.size() - 24）
        if len(qr_data) >= 24:
            return qr_data[-24:]
        # 回退: 从 ticket= 参数提取
        if "ticket=" in qr_data:
            return qr_data.split("ticket=")[-1][:24]
        return ""

    def _detect_qr(self, img: np.ndarray) -> str:
        """
        检测并解码二维码（优先使用 pyzbar，多重预处理策略）
        """
        # 策略1：pyzbar 直接解码原始图
        if USE_PYZBAR:
            try:
                img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                decoded_objects = qr_decode(img_rgb, symbols=[ZBarSymbol.QRCODE])
                if decoded_objects:
                    return decoded_objects[0].data.decode('utf-8')
            except Exception as e:
                scanner_log(f"pyzbar 解码异常: {e}", LogLevel.WARN)

        # 策略2-4：对图像进行不同预处理后用 pyzbar 重试
        if USE_PYZBAR:
            # 策略2：灰度 + 自适应二值化
            try:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                               cv2.THRESH_BINARY, 21, 5)
                img_rgb = cv2.cvtColor(binary, cv2.COLOR_GRAY2RGB)
                decoded_objects = qr_decode(img_rgb, symbols=[ZBarSymbol.QRCODE])
                if decoded_objects:
                    return decoded_objects[0].data.decode('utf-8')
            except Exception:
                pass

            # 策略3：放大2倍后解码
            try:
                h, w = img.shape[:2]
                scaled = cv2.resize(img, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
                img_rgb = cv2.cvtColor(scaled, cv2.COLOR_BGR2RGB)
                decoded_objects = qr_decode(img_rgb, symbols=[ZBarSymbol.QRCODE])
                if decoded_objects:
                    return decoded_objects[0].data.decode('utf-8')
            except Exception:
                pass

            # 策略4：灰度 + 锐化
            try:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                kernel = np.array([[-1,-1,-1], [-1,9,-1], [-1,-1,-1]])
                sharpened = cv2.filter2D(gray, -1, kernel)
                img_rgb = cv2.cvtColor(sharpened, cv2.COLOR_GRAY2RGB)
                decoded_objects = qr_decode(img_rgb, symbols=[ZBarSymbol.QRCODE])
                if decoded_objects:
                    return decoded_objects[0].data.decode('utf-8')
            except Exception:
                pass

        # 降级使用 OpenCV
        try:
            vertices = self.detector.detect(img)
            if vertices is not None and len(vertices) > 0:
                scanner_log("OpenCV 检测到可能的二维码区域，但解码失败", LogLevel.WARN)
            qr_data, _, _ = self.detector.detectAndDecode(img)
            if qr_data:
                return qr_data
        except Exception as e:
            scanner_log(f"OpenCV 解码异常: {e}", LogLevel.WARN)

        return ""


class StreamScanner(QThread):
    """
    直播流二维码扫描器
    从直播流中检测并解析二维码
    """
    qrcode_detected = pyqtSignal(str)
    qrcode_game_detected = pyqtSignal(str, int, int)  # (url, game_type_val, app_id)
    scan_finished = pyqtSignal(bool)
    scan_error = pyqtSignal(str)
    stream_status = pyqtSignal(str)  # 流状态更新
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = False
        self._stream_url = ""
        self._last_ticket = ""
        self._frame_count = 0
        self._used_tickets = set()  # 已处理的ticket，防止重复检测
        self._headers = {}          # 打开直播流所需的 HTTP 头（对齐 C++ GetStreamLink 的 heards）
        self._proc = None           # ffmpeg 子进程（对齐 C++ avformat_open_input）
        
        # 二维码检测器
        self.detector = cv2.QRCodeDetector()
    
    def set_stream_url(self, url: str):
        """设置直播流URL"""
        self._stream_url = url

    def set_headers(self, headers: dict):
        """设置打开直播流所需的 HTTP 头（对齐 C++ GetStreamLink：B站流必须带 Referer/Origin/User-Agent）"""
        self._headers = headers or {}
    
    def stop(self):
        """停止扫描"""
        self._running = False
        self._cleanup_proc()
    
    def _cleanup_proc(self):
        if self._proc is not None:
            try:
                self._proc.terminate()
                if self._proc.stdout:
                    self._proc.stdout.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def run(self):
        """开始扫描（读帧/检测分离：reader 线程只管以最快速度拉取并保留最新一帧，
        检测线程永远解码「当前画面」，避免 ffmpeg 管道缓冲堆积造成延迟、扫不上码）"""
        self._running = True
        self._last_ticket = ""
        self._frame_count = 0

        if not self._stream_url:
            self.scan_error.emit("未设置直播流URL")
            self.scan_finished.emit(False)
            return

        # 对齐 C++ QRCodeForStream：优先用 ffmpeg（avformat_open_input）打开直播流，
        # B站流需带 Referer/Origin/User-Agent 头；无 ffmpeg 时回退 cv2.VideoCapture。
        frame_iter = None
        open_err = ""
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            frame_iter, open_err = self._make_ffmpeg_iter(ffmpeg)
            if frame_iter is None:
                scanner_log(f"[StreamScanner] ffmpeg 打开失败: {open_err}", LogLevel.WARN)
        if frame_iter is None:
            frame_iter, open_err = self._make_cv2_iter()
            if frame_iter is None:
                self.scan_error.emit("无法打开直播流")
                self.scan_finished.emit(False)
                return

        self.stream_status.emit("正在连接直播流...")

        # 最新帧队列：最多保留 1 帧，reader 拉到新帧时挤掉旧帧，保证检测基于当前画面
        latest = queue.Queue(maxsize=1)

        def reader():
            try:
                for frame in frame_iter:
                    if not self._running:
                        break
                    if latest.full():
                        try:
                            latest.get_nowait()
                        except queue.Empty:
                            pass
                    latest.put(frame)
            except Exception as e:
                scanner_log(f"[StreamScanner] reader 异常: {e}", LogLevel.WARN)
            finally:
                # 流断开即通知主循环退出
                self._running = False

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        while self._running:
            try:
                frame = latest.get(timeout=2)
            except queue.Empty:
                continue

            self._frame_count += 1

            # ffmpeg 路径已统一缩放到 1280x720；cv2 路径按需缩放
            if frame.shape[1] > 1280 or frame.shape[0] > 720:
                h, w = frame.shape[:2]
                scale = min(1280 / w if w > 1280 else 1.0, 720 / h if h > 720 else 1.0)
                if scale != 1.0:
                    frame = cv2.resize(frame, (int(w * scale), int(h * scale)))

            # 检测并解码二维码（每帧都检测，不再跳帧）
            qr_data = self._detect_qr(frame)

            # C++ 风格二维码验证：URL长度 >= 85 且 offset 79 匹配已知游戏
            if qr_data and len(qr_data) >= 85:
                game_type, app_id = get_game_type_from_url(qr_data)
                if game_type == GameType.UNKNOW:
                    continue

                # C++ 风格: ticket = URL 最后24个字符
                ticket = qr_data[-24:]

                # C++ 风格: 防重复 lastTicket
                if ticket and ticket == self._last_ticket:
                    continue
                self._last_ticket = ticket

                scanner_log(f"直播流检测到游戏二维码 (帧数={self._frame_count}, 类型={game_type.name}, app_id={app_id})")
                self.qrcode_game_detected.emit(qr_data, int(game_type), app_id)
                self.qrcode_detected.emit(qr_data)

        t.join(timeout=2)
        self.scan_finished.emit(True)

    def _make_ffmpeg_iter(self, ffmpeg: str):
        """用 ffmpeg 子进程管道打开直播流（对齐 C++ setUrl + avformat_open_input）。
        返回 (生成器, None) 成功，或 (None, 错误描述) 失败。"""
        try:
            W, H = 1280, 720
            # 对齐 C++ QRCodeForStream::setUrl 的 ffmpeg 选项
            cmd = [
                ffmpeg, "-v", "error",
                "-rw_timeout", "5000000",   # 读超时 5s
                "-probesize", "1024",
                "-max_delay", "0",
                "-fflags", "+nobuffer",
                "-flags", "low_delay",
            ]
            # 对齐 C++ GetStreamLink：B站流带 Referer/Origin/User-Agent
            ua = self._headers.get("User-Agent")
            if ua:
                cmd += ["-user_agent", ua]
            if self._headers:
                hdr = "".join(f"{k}: {v}\r\n" for k, v in self._headers.items())
                cmd += ["-headers", hdr]
            cmd += ["-i", self._stream_url,
                    "-vf", f"scale={W}:{H}",
                    "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]

            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10 ** 7
            )

            frame_size = W * H * 3
            first = self._proc.stdout.read(frame_size)
            if not first or len(first) < frame_size:
                rc = self._proc.poll()
                self._cleanup_proc()
                return None, f"ffmpeg 打开失败(退出码 {rc})" if rc is not None else "ffmpeg 读取首帧失败"

            first_frame = np.frombuffer(first, dtype=np.uint8).reshape(H, W, 3)

            def gen():
                try:
                    yield first_frame
                    while self._running:
                        raw = self._proc.stdout.read(frame_size)
                        if not raw or len(raw) < frame_size:
                            self.stream_status.emit("直播流已断开")
                            break
                        yield np.frombuffer(raw, dtype=np.uint8).reshape(H, W, 3)
                finally:
                    self._cleanup_proc()

            return gen(), None
        except Exception as e:
            self._cleanup_proc()
            return None, str(e)

    def _make_cv2_iter(self):
        """回退：用 OpenCV 打开直播流（无法携带自定义 HTTP 头）"""
        cap = cv2.VideoCapture(self._stream_url)
        if not cap.isOpened():
            cap.release()
            return None, "cv2 无法打开直播流"

        def gen():
            try:
                while self._running:
                    ret, frame = cap.read()
                    if not ret:
                        self.stream_status.emit("直播流已断开")
                        break
                    yield frame
            finally:
                cap.release()

        return gen(), None
    
    def reset_last_qr(self):
        """重置上一个二维码数据"""
        self._last_ticket = ""

    def clear_used_tickets(self):
        """清除已使用的ticket记录"""
        self._used_tickets.clear()
        self._last_ticket = ""

    def _extract_ticket(self, qr_data: str) -> str:
        """从二维码URL中提取ticket"""
        if "ticket=" in qr_data:
            return qr_data.split("ticket=")[-1][:24]
        return ""

    def _detect_qr(self, img: np.ndarray) -> str:
        """检测并解码二维码（轻量：pyzbar 直接解码 + 灰度二值化重试 + OpenCV 兜底；
        不做 2 倍放大等重策略，保证直播检测实时性）"""
        if USE_PYZBAR:
            try:
                img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                decoded_objects = qr_decode(img_rgb, symbols=[ZBarSymbol.QRCODE])
                if decoded_objects:
                    return decoded_objects[0].data.decode('utf-8')
            except Exception:
                pass

            # 轻量重试：灰度 + 自适应二值化（直播画面常有压缩噪点，成本很低）
            try:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                               cv2.THRESH_BINARY, 21, 5)
                img_rgb = cv2.cvtColor(binary, cv2.COLOR_GRAY2RGB)
                decoded_objects = qr_decode(img_rgb, symbols=[ZBarSymbol.QRCODE])
                if decoded_objects:
                    return decoded_objects[0].data.decode('utf-8')
            except Exception:
                pass

        try:
            qr_data, vertices, _ = self.detector.detectAndDecode(img)
            if qr_data:
                return qr_data
        except Exception:
            pass

        return ""


def decode_qr_from_image(img: np.ndarray) -> Tuple[bool, str]:
    """
    从图像中解码二维码

    Args:
        img: OpenCV图像 (BGR格式)

    Returns:
        (成功, 二维码内容)
    """
    if USE_PYZBAR:
        try:
            img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            decoded_objects = qr_decode(img_rgb, symbols=[ZBarSymbol.QRCODE])
            if decoded_objects:
                return True, decoded_objects[0].data.decode('utf-8')
        except Exception:
            pass

    detector = cv2.QRCodeDetector()
    data, vertices, _ = detector.detectAndDecode(img)

    if data:
        return True, data
    return False, ""


def decode_qr_from_file(file_path: str) -> Tuple[bool, str]:
    """
    从文件中解码二维码

    Args:
        file_path: 图像文件路径

    Returns:
        (成功, 二维码内容)
    """
    img = cv2.imread(file_path)
    if img is None:
        return False, ""
    return decode_qr_from_image(img)


def is_mhy_qrcode(qr_data: str) -> Tuple[bool, str]:
    """
    检查是否是米哈游游戏的二维码

    Returns:
        (是否米哈游二维码, 游戏类型标识)
    """
    if not qr_data:
        debug("[is_mhy_qrcode] 二维码数据为空")
        return False, ""

    if len(qr_data) < 10:
        debug(f"[is_mhy_qrcode] 二维码长度过短: {len(qr_data)}")
        return False, ""

    debug(f"[is_mhy_qrcode] 检查二维码 (长度={len(qr_data)})")
    debug(f"[is_mhy_qrcode] 内容预览: {qr_data[:100]}...")

    # 米哈游二维码通常包含特定域名
    mhy_domains = [
        "hk4e-sdk.mihoyo.com",
        "hkrpg-sdk.mihoyo.com",
        "bh3-sdk.mihoyo.com",
        "zzz-sdk.mihoyo.com",
        "sdk-os.mihoyo.com",
        "sdk.hyk4.com",
        "user.mihoyo.com",
        "hk4e-api.mihoyo.com",
        "mihoyo.com",
        "hoyoverse.com",
        "bilibili.com",           # B站登录
        "lg.bilibiligame.net",   # B站游戏
    ]

    for domain in mhy_domains:
        if domain in qr_data:
            debug(f"[is_mhy_qrcode] 匹配到: {domain}")
            # 判断具体游戏
            if "hkrpg" in qr_data or "sr" in qr_data.lower():
                return True, "hkrpg"
            elif "zzz" in qr_data or "jss" in qr_data.lower():
                return True, "zzz"
            elif "bh3" in qr_data:
                return True, "bh3"
            elif "genshin" in qr_data or "hk4e" in qr_data:
                return True, "genshin"
            return True, ""

    # 检查是否包含ticket参数（二维码登录）
    if "ticket=" in qr_data:
        debug(f"[is_mhy_qrcode] 匹配到ticket参数，返回True")
        return True, ""

    debug(f"[is_mhy_qrcode] 不是米哈游二维码 (内容: {qr_data[:80]})")
    return False, ""


def extract_ticket(qr_data: str) -> Tuple[str, int]:
    """
    从二维码URL中提取ticket和app_id

    Args:
        qr_data: 二维码内容(URL)

    Returns:
        (ticket字符串, app_id)
        - 程序生成的二维码: ticket, app_id=1
        - 游戏内二维码: ticket, app_id (从URL中解析)
    """
    try:
        ticket = ""
        app_id = 1

        # 提取ticket
        if "ticket=" in qr_data:
            ticket_part = qr_data.split("ticket=")[-1]
            # ticket通常是24位
            ticket = ticket_part[:24]

        # 提取app_id（游戏内二维码）
        if "app_id=" in qr_data:
            app_id_part = qr_data.split("app_id=")[-1]
            # app_id通常是数字
            app_id_str = app_id_part.split("&")[0]
            try:
                app_id = int(app_id_str)
            except ValueError:
                app_id = 1

        return ticket, app_id
    except Exception:
        return "", 1
