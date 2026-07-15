"""
登录窗口模块
支持4种登录方式：
1. 短信登录 - 手机号+验证码
2. 扫码登录 - 生成二维码供米游社APP扫码
3. Cookie登录 - 粘贴SToken Cookie
4. B站崩坏3登录 - 账号密码
"""

import time
import json
import random
import traceback
import threading
import qrcode
from io import BytesIO
from PyQt6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QLabel, QLineEdit, QPushButton, QMessageBox,
    QCheckBox
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread, QObject
from api import MhyApi, ServerType, GameType, LoginQRCodeState, BSGameSDK
from core.config import Account
from core.logger import qr_log, gui_log, error, LogLevel
from PyQt6.QtGui import QPixmap, QImage, QFont, QCursor


class QRCodeCheckWorker(QObject):
    """二维码状态检查 Worker（hoyolab passport API 版）

    使用 hoyolab passport API:
    - POST createQRLogin → 获取二维码 (url + ticket)
    - POST queryQRLoginStatus → 轮询状态 (Created/Scanned/Confirmed)
    - Confirmed 时 token 通过 Set-Cookie 头直接返回，无需额外转换
    """
    state_changed = pyqtSignal(str, str, str, str)  # (state_name, uid, mid, token)
    finished = pyqtSignal()

    def __init__(self, api: MhyApi, ticket: str):
        super().__init__()
        self.api = api
        self.ticket = ticket
        self._running = False

    def start_check(self):
        self._running = True

    def stop_check(self):
        self._running = False

    def run(self):
        """每 1.5 秒轮询 hoyolab passport QR 状态"""
        base_interval = 1.5
        while self._running:
            try:
                status, uid, mid, token = self.api.query_hoyolab_qrcode_status(self.ticket)
                self.state_changed.emit(status, uid, mid, token)

                if status in ("Confirmed", "Expired"):
                    self._running = False
                    break

                time.sleep(base_interval + random.random() * 0.5)
            except Exception as e:
                qr_log(f"检查二维码状态异常: {e}", LogLevel.WARN)
                time.sleep(base_interval)
        self.finished.emit()


class LoginWindow(QDialog):
    """登录窗口"""

    # 登录成功信号
    login_success = pyqtSignal(str, str, str, str, str)  # (name, token, uid, mid, server_type)

    # 短信发送结果信号
    sms_send_result = pyqtSignal(bool, str)  # (success, message_or_action_type)

    # 短信登录结果信号（与login_success一致）
    sms_login_result = pyqtSignal(bool, str, str, str, str)  # (success, msg, uid, token, mid)

    # 二维码信号
    qr_fetched = pyqtSignal(str, str)  # (url, ticket)
    qr_fetch_error = pyqtSignal(str)   # (error_message)

    # B站登录结果信号
    bh3_login_result = pyqtSignal(bool, str, str, str, str)  # (success, msg, uid, token, server)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = MhyApi()
        self.setWindowTitle("添加账号")
        self.setMinimumSize(450, 550)
        self.setWindowFlags(Qt.WindowType.Dialog)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)

        self.qr_check_thread: QThread = None
        self.qr_check_worker: QRCodeCheckWorker = None
        self.qr_fetch_thread: threading.Thread = None  # 用于跟踪获取二维码的线程
        self._qr_fetching = False  # 防止并发 fetch
        self.current_ticket = ""
        self.remaining_seconds = 0
        self.sms_action_type = ""

        self.init_ui()

        # 连接短信信号
        self.sms_send_result.connect(self._on_sms_send_result)
        self.sms_login_result.connect(self._on_sms_login_result)

        # 连接二维码信号
        self.qr_fetched.connect(self._on_qr_fetched)
        self.qr_fetch_error.connect(self._on_qr_fetch_error)

        # 连接B站登录信号
        self.bh3_login_result.connect(self._on_bh3_login_result)

        # 移动窗口到鼠标当前位置的水平居中处
        self.move_to_mouse_center()

    def move_to_mouse_center(self):
        """移动窗口到鼠标当前位置的水平居中处"""
        mouse_pos = QCursor.pos()
        x = mouse_pos.x() - self.width() // 2
        y = mouse_pos.y()
        self.move(x, y)

    def init_ui(self):
        """初始化UI"""
        layout = QVBoxLayout(self)

        # 标签页
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # Tab 0: 短信登录
        self.tab_sms = self.create_sms_tab()
        self.tabs.addTab(self.tab_sms, "短信登录")
        self.tabs.tabBar().setTabVisible(0, False)

        # Tab 1: 扫码登录
        self.tab_qr = self.create_qr_tab()
        self.tabs.addTab(self.tab_qr, "扫码登录")
        self.tabs.tabBar().setTabVisible(1, True)

        # Tab 2: Cookie登录
        self.tab_cookie = self.create_cookie_tab()
        self.tabs.addTab(self.tab_cookie, "Cookie登录")
        self.tabs.tabBar().setTabVisible(2, True)

        # Tab 3: B站崩坏3登录
        self.tab_bh3 = self.create_bh3_tab()
        self.tabs.addTab(self.tab_bh3, "B站崩坏3")
        self.tabs.tabBar().setTabVisible(3, True)

        # 默认选中第一个可见标签页
        for i in range(self.tabs.count()):
            if self.tabs.tabBar().isTabVisible(i):
                self.tabs.setCurrentIndex(i)
                break

        # 连接信号
        self.tabs.currentChanged.connect(self.on_tab_changed)

    def create_sms_tab(self) -> QWidget:
        """创建短信登录页面"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(20, 30, 20, 20)

        # 手机号输入
        phone_layout = QHBoxLayout()
        phone_layout.addWidget(QLabel("+86"))
        self.sms_phone = QLineEdit()
        self.sms_phone.setPlaceholderText("请输入手机号码")
        self.sms_phone.setFixedHeight(45)
        self.sms_phone.setFont(QFont("", 13))
        phone_layout.addWidget(self.sms_phone)

        self.sms_send_btn = QPushButton("发送")
        self.sms_send_btn.setFixedSize(60, 45)
        self.sms_send_btn.setEnabled(False)
        phone_layout.addWidget(self.sms_send_btn)
        layout.addLayout(phone_layout)

        # 验证码输入
        self.sms_code = QLineEdit()
        self.sms_code.setPlaceholderText("请输入验证码")
        self.sms_code.setFixedHeight(45)
        self.sms_code.setFont(QFont("", 13))
        layout.addWidget(self.sms_code)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.sms_confirm_btn = QPushButton("确认登录")
        self.sms_confirm_btn.setFixedSize(120, 40)
        self.sms_confirm_btn.setEnabled(False)
        btn_layout.addWidget(self.sms_confirm_btn)

        self.sms_cancel_btn = QPushButton("取消")
        self.sms_cancel_btn.setFixedSize(120, 40)
        btn_layout.addWidget(self.sms_cancel_btn)
        btn_layout.addStretch()

        layout.addLayout(btn_layout)
        layout.addStretch()

        # 信号连接
        self.sms_phone.textChanged.connect(self.on_sms_phone_changed)
        self.sms_send_btn.clicked.connect(self.on_sms_send)
        self.sms_confirm_btn.clicked.connect(self.on_sms_confirm)
        self.sms_cancel_btn.clicked.connect(self.close)

        return widget

    def create_qr_tab(self) -> QWidget:
        """创建扫码登录页面"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(20, 20, 20, 20)

        # 提示
        self.qr_prompt = QLabel("打开米游社APP，扫一扫登录")
        self.qr_prompt.setFont(QFont("", 14))
        self.qr_prompt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.qr_prompt)

        # 二维码显示区
        self.qr_label = QLabel()
        self.qr_label.setFixedSize(250, 250)
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setStyleSheet("border: 2px solid #ccc; background: white;")
        self.qr_label.setText("点击下方按钮刷新二维码")
        qr_hlayout = QHBoxLayout()
        qr_hlayout.addStretch()
        qr_hlayout.addWidget(self.qr_label)
        qr_hlayout.addStretch()
        layout.addLayout(qr_hlayout)

        # 刷新按钮
        self.qr_refresh_btn = QPushButton("刷新二维码")
        self.qr_refresh_btn.setFixedSize(150, 40)
        btn_hlayout = QHBoxLayout()
        btn_hlayout.addStretch()
        btn_hlayout.addWidget(self.qr_refresh_btn)
        btn_hlayout.addStretch()
        layout.addLayout(btn_hlayout)

        # 刷新提示
        self.qr_tip = QLabel("二维码有效期约5分钟")
        self.qr_tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_tip.setStyleSheet("color: #888;")
        tip_hlayout = QHBoxLayout()
        tip_hlayout.addStretch()
        tip_hlayout.addWidget(self.qr_tip)
        tip_hlayout.addStretch()
        layout.addLayout(tip_hlayout)

        layout.addStretch()

        # 信号连接
        self.qr_refresh_btn.clicked.connect(self.on_qr_refresh)
        self.tabs.currentChanged.connect(self.on_qr_tab_shown)

        return widget

    def create_cookie_tab(self) -> QWidget:
        """创建Cookie登录页面"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(20, 40, 20, 20)

        # 说明
        info_label = QLabel("请粘贴包含SToken的Cookie\n（需包含 stuid, stoken, mid 字段）")
        info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info_label.setStyleSheet("color: #666; padding: 10px;")
        layout.addWidget(info_label)

        # Cookie输入
        self.cookie_input = QLineEdit()
        self.cookie_input.setPlaceholderText("在这里粘贴Cookie...")
        self.cookie_input.setFixedHeight(50)
        self.cookie_input.setFont(QFont("", 12))
        layout.addWidget(self.cookie_input)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.cookie_login_btn = QPushButton("登录")
        self.cookie_login_btn.setFixedSize(100, 40)
        btn_layout.addWidget(self.cookie_login_btn)

        self.cookie_cancel_btn = QPushButton("取消")
        self.cookie_cancel_btn.setFixedSize(100, 40)
        btn_layout.addWidget(self.cookie_cancel_btn)
        btn_layout.addStretch()

        layout.addLayout(btn_layout)
        layout.addStretch()

        # 信号连接
        self.cookie_login_btn.clicked.connect(self.on_cookie_login)
        self.cookie_cancel_btn.clicked.connect(self.close)

        return widget

    def create_bh3_tab(self) -> QWidget:
        """创建B站崩坏3登录页面"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(20, 40, 20, 20)

        # 说明
        info_label = QLabel("请输入B站崩坏3的账号密码")
        info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(info_label)

        # 账号
        self.bh3_account = QLineEdit()
        self.bh3_account.setPlaceholderText("请输入B站账号")
        self.bh3_account.setFixedHeight(45)
        layout.addWidget(self.bh3_account)

        # 密码
        self.bh3_password = QLineEdit()
        self.bh3_password.setPlaceholderText("请输入密码")
        self.bh3_password.setFixedHeight(45)
        self.bh3_password.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.bh3_password)

        # 显示密码
        self.bh3_show_pwd = QCheckBox("显示密码")
        layout.addWidget(self.bh3_show_pwd)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.bh3_login_btn = QPushButton("登录")
        self.bh3_login_btn.setFixedSize(100, 40)
        btn_layout.addWidget(self.bh3_login_btn)

        self.bh3_cancel_btn = QPushButton("取消")
        self.bh3_cancel_btn.setFixedSize(100, 40)
        btn_layout.addWidget(self.bh3_cancel_btn)
        btn_layout.addStretch()

        layout.addLayout(btn_layout)
        layout.addStretch()

        # 信号连接
        self.bh3_show_pwd.toggled.connect(self.on_bh3_show_pwd)
        self.bh3_login_btn.clicked.connect(self.on_bh3_login)
        self.bh3_cancel_btn.clicked.connect(self.close)

        return widget

    # ========== 信号处理 ==========

    def on_tab_changed(self, index: int):
        """标签页切换"""
        if index == 1:  # 扫码登录
            self.on_qr_refresh()

    def on_qr_tab_shown(self, index: int):
        """扫码标签页显示"""
        if index == 1:
            self.on_qr_refresh()

    def on_sms_phone_changed(self, text: str):
        """手机号输入变化"""
        self.sms_send_btn.setEnabled(len(text) >= 11)

    def on_sms_send(self):
        """发送验证码"""
        phone = self.sms_phone.text().strip()
        if not phone or len(phone) < 11:
            gui_log("请输入正确的手机号", LogLevel.WARN)
            QMessageBox.warning(self, "错误", "请输入正确的手机号")
            return

        self.sms_send_btn.setEnabled(False)
        self.sms_send_btn.setText("发送中...")

        def do_send():
            retcode, action_type, extra = self.api.send_sms_code(phone)

            if retcode == 0:
                self.sms_action_type = action_type
                self.sms_send_result.emit(True, action_type)
            elif retcode == -3002:
                self.sms_send_result.emit(False, f"发送过于频繁: {action_type}")
            else:
                self.sms_send_result.emit(False, f"发送失败 (code={retcode})\n\n提示：可能触发了风控，请等待1-2分钟后重试，或使用扫码登录")

        threading.Thread(target=do_send, daemon=True).start()

    def _on_sms_send_result(self, success: bool, message: str):
        """短信发送结果处理（在主线程）"""
        if success:
            self.remaining_seconds = 60
            self.sms_confirm_btn.setEnabled(True)
            self.sms_send_btn.setText("发送")
            self.sms_send_btn.setEnabled(True)
            self.update_sms_timer()
            gui_log("验证码已发送")
            QMessageBox.information(self, "提示", "验证码已发送")
        else:
            self.sms_send_btn.setText("发送")
            self.sms_send_btn.setEnabled(True)
            gui_log(f"短信发送失败: {message}", LogLevel.WARN)
            QMessageBox.warning(self, "错误", message)

    def update_sms_timer(self):
        """更新短信倒计时"""
        if self.remaining_seconds > 0:
            self.sms_send_btn.setText(f"{self.remaining_seconds}秒")
            self.remaining_seconds -= 1
            QTimer.singleShot(1000, self.update_sms_timer)
        else:
            self.sms_send_btn.setText("发送")
            self.sms_send_btn.setEnabled(True)

    def on_sms_confirm(self):
        """短信登录确认"""
        phone = self.sms_phone.text().strip()
        code = self.sms_code.text().strip()

        if not code:
            gui_log("请输入验证码", LogLevel.WARN)
            QMessageBox.warning(self, "错误", "请输入验证码")
            return

        self.sms_confirm_btn.setEnabled(False)
        self.sms_confirm_btn.setText("登录中...")

        def do_login():
            # 调用新增的5返回值方法，与定义完全对应
            retcode, cookie_token, stoken, uid, mid = self.api.login_by_sms_get_stoken(
                phone, code, getattr(self, "sms_action_type", "")
            )

            if retcode == 0:
                # 优先传递 stoken，为空则用 cookie_token 兜底
                token_to_pass = stoken if stoken else cookie_token
                self.sms_login_result.emit(True, "", uid, token_to_pass, mid)
            elif retcode == -3205:
                self.sms_login_result.emit(False, "验证码错误", "", "", "")
            else:
                self.sms_login_result.emit(False, f"登录失败 (code={retcode})", "", "", "")

        # 启动子线程执行登录
        threading.Thread(target=do_login, daemon=True).start()

    def _on_sms_login_result(self, success: bool, msg: str, uid: str, token: str, mid: str):
        """短信登录结果处理（在主线程）"""
        if success:
            name = self.api.get_mys_user_name(uid)
            if not name:
                name = f"用户{uid}"

            gui_log(f"短信登录成功！用户名: {name}")
            QMessageBox.information(self, "成功", f"登录成功！\n用户名: {name}")
            self.login_success.emit(name, token, uid, mid, "官服")
            self.close()
        else:
            self.sms_confirm_btn.setText("确认登录")
            self.sms_confirm_btn.setEnabled(True)
            gui_log(f"短信登录失败: {msg}", LogLevel.WARN)
            QMessageBox.warning(self, "错误", msg)

    def on_qr_refresh(self):
        """刷新二维码"""
        # 防止并发 fetch（currentChanged 可能被多次触发）
        if self._qr_fetching:
            return
        self._qr_fetching = True

        # 停止之前的检查
        self._cleanup_qr_thread()

        self.qr_label.setText("正在获取二维码...")

        def fetch_qr():
            try:
                url, ticket = self.api.fetch_hoyolab_qrcode()
                # 通过信号在主线程更新UI
                self.qr_fetched.emit(url, ticket)
            except Exception as e:
                self.qr_fetch_error.emit(str(e))
            finally:
                self._qr_fetching = False

        self.qr_fetch_thread = threading.Thread(target=fetch_qr, daemon=True)
        self.qr_fetch_thread.start()

    def _on_qr_fetched(self, url: str, ticket: str):
        """二维码获取成功"""
        if url and ticket:
            # 清除过期状态的遮罩效果（对齐 C++ StartQRCodeLogin: UpdateQrcodeButton->hide()）
            self.qr_label.setGraphicsEffect(None)
            self.qr_prompt.setStyleSheet("")  # 清除过期红色样式

            self.current_ticket = ticket
            self.generate_qr_image(url)
            self.start_qr_check()
        else:
            self.qr_label.setText("获取二维码失败\n点击刷新")

    def _on_qr_fetch_error(self, error: str):
        """二维码获取失败"""
        self.qr_label.setText(f"错误: {error[:20]}\n点击刷新")

    def _cleanup_qr_thread(self):
        """清理二维码检查线程"""
        # 先停止 worker，防止新的检查循环
        if self.qr_check_worker:
            self.qr_check_worker.stop_check()

        # 保存线程引用
        thread = self.qr_check_thread
        self.qr_check_worker = None
        self.qr_check_thread = None

        # 等待线程结束
        if thread is not None:
            if thread.isRunning():
                thread.quit()
                # worker 可能在 time.sleep() 中，需要等待才能响应 quit
                if not thread.wait(3000):
                    # 3秒后仍未结束，强制终止
                    thread.terminate()
                    thread.wait(1000)
            # 线程完全停止后再调度删除，避免 Destroyed while still running
            if not thread.isRunning():
                thread.deleteLater()

    def generate_qr_image(self, url: str):
        """生成二维码图片"""
        try:
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=2
            )
            qr.add_data(url)
            qr.make(fit=True)

            img = qr.make_image(fill_color="black", back_color="white")

            buffer = BytesIO()
            img.save(buffer, format='PNG')
            buffer.seek(0)

            image = QImage.fromData(buffer.getvalue())
            pixmap = QPixmap.fromImage(image)

            self.qr_label.setPixmap(pixmap.scaled(250, 250, Qt.AspectRatioMode.KeepAspectRatio))
            self.qr_prompt.setText("打开米游社APP，扫一扫登录")

        except Exception as e:
            self.qr_label.setText(f"二维码生成失败\n{str(e)}")

    def start_qr_check(self):
        """开始检查二维码状态"""
        if not self.current_ticket:
            return

        # 清理之前的线程
        self._cleanup_qr_thread()

        # 创建 worker 和线程（对齐 C++：仅传 api 和 ticket）
        self.qr_check_worker = QRCodeCheckWorker(self.api, self.current_ticket)
        self.qr_check_thread = QThread()
        self.qr_check_worker.moveToThread(self.qr_check_thread)

        # 连接信号
        self.qr_check_thread.started.connect(self.qr_check_worker.run)
        self.qr_check_worker.state_changed.connect(self.on_qr_state_changed)
        self.qr_check_worker.finished.connect(self.on_qr_check_finished)
        self.qr_check_thread.finished.connect(self.on_qr_thread_finished)

        self.qr_check_worker.start_check()
        self.qr_check_thread.start()

    def on_qr_check_finished(self):
        """二维码检查完成"""
        pass

    def on_qr_thread_finished(self):
        """二维码检查线程结束"""
        pass

    def on_qr_state_changed(self, state_name: str, uid: str, mid: str, token: str):
        """二维码状态变化（hoyolab passport API）
        状态名: Created / Scanned / Confirmed / Expired
        """
        if state_name == "Created":
            pass
        elif state_name == "Scanned":
            # 对齐 C++: "正在登录\n\n请在手机上点击「确认登录」"
            self.qr_prompt.setText("正在登录\n\n请在手机上点击「确认登录」")
        elif state_name == "Confirmed":
            self.on_qr_confirmed(uid, mid, token)
        elif state_name == "Expired":
            self._on_qr_expired()

    def on_qr_confirmed(self, uid: str, mid: str, token: str):
        """二维码确认登录（hoyolab passport API — Token 由 Set-Cookie 直接返回）"""
        try:
            if not token:
                qr_log("确认登录失败: token 为空", LogLevel.ERROR)
                gui_log("获取 Token 失败！请重试。", LogLevel.ERROR)
                QMessageBox.warning(self, "错误", "获取 Token 失败！\n请重试或使用 Cookie 登录。")
                self.qr_prompt.setText("登录失败\n点击刷新重试")
                return

            qr_log(f"uid={uid}, mid={mid}, token={token[:16]}...", LogLevel.DEBUG)

            # 获取用户名
            name = self.api.get_mys_user_name(uid)
            if not name:
                name = f"用户{uid}"

            # 发送登录成功信号
            self.qr_prompt.setText("登录成功！")
            self.login_success.emit(name, token, uid, mid, "官服")
            self.close()

        except Exception as e:
            error(f"登录处理异常: {e}\n{traceback.format_exc()}")
            gui_log(f"登录处理异常: {str(e)}", LogLevel.ERROR)
            QMessageBox.warning(self, "错误", f"登录处理异常: {str(e)}")

    def _on_qr_expired(self):
        """二维码过期处理（对齐 C++ QrcodeLoginResult(false)）"""
        from PyQt6.QtWidgets import QGraphicsOpacityEffect

        # 对齐 C++: QRCodeQImage = CV_8UC1_MatToQImage(QrcodeMat - cv::Scalar(200))
        # 给二维码添加半透明遮罩效果，表示已过期
        if self.qr_label.graphicsEffect() is None:
            effect = QGraphicsOpacityEffect()
            effect.setOpacity(0.35)
            self.qr_label.setGraphicsEffect(effect)

        # 更新提示文字
        self.qr_prompt.setText("二维码已过期\n点击刷新二维码")
        # 设置样式为警告色（与 C++ 按钮文案一致）
        self.qr_prompt.setStyleSheet("color: #e53935; font-size: 14px;")

    def on_cookie_login(self):
        """Cookie登录"""
        cookie_str = self.cookie_input.text().strip()

        if not cookie_str:
            gui_log("请粘贴Cookie", LogLevel.WARN)
            QMessageBox.warning(self, "错误", "请粘贴Cookie")
            return

        try:
            uid, stoken, mid = self.parse_cookie(cookie_str)

            if not all([uid, stoken, mid]):
                gui_log("Cookie格式错误，缺少必要字段", LogLevel.WARN)
                QMessageBox.warning(self, "错误", "Cookie格式错误，缺少必要字段")
                return

            name = self.api.get_mys_user_name(uid)
            if not name:
                name = f"用户{uid}"

            gui_log(f"Cookie登录成功！用户名: {name}")
            QMessageBox.information(self, "成功", f"登录成功！\n用户名: {name}")

            self.login_success.emit(name, stoken, uid, mid, "官服")
            self.close()

        except Exception as e:
            gui_log(f"Cookie解析失败: {str(e)}", LogLevel.WARN)
            QMessageBox.warning(self, "错误", f"Cookie解析失败: {str(e)}")

    def parse_cookie(self, cookie_str: str) -> tuple:
        """解析Cookie字符串"""
        uid = ""
        stoken = ""
        mid = ""

        # 尝试解析JSON格式
        if cookie_str.startswith('{'):
            try:
                data = json.loads(cookie_str)
                uid = str(data.get('stuid', data.get('ltuid', data.get('account_id', ''))))
                stoken = data.get('stoken', '')
                mid = data.get('mid', '')
                return uid, stoken, mid
            except:
                pass

        # 尝试解析key=value格式
        parts = cookie_str.split(';')
        for part in parts:
            part = part.strip()
            if '=' in part:
                key, value = part.split('=', 1)
                key = key.strip().lower()
                value = value.strip()

                if key in ['stuid', 'ltuid', 'account_id'] and not uid:
                    uid = value
                elif key == 'stoken':
                    stoken = value
                elif key == 'mid':
                    mid = value

        return uid, stoken, mid

    def on_bh3_show_pwd(self, checked: bool):
        """显示密码切换"""
        if checked:
            self.bh3_password.setEchoMode(QLineEdit.EchoMode.Normal)
        else:
            self.bh3_password.setEchoMode(QLineEdit.EchoMode.Password)

    def on_bh3_login(self):
        """B站崩坏3登录（参考 C++ WindowLogin.cpp）"""
        self._bh3_account = self.bh3_account.text().strip()
        self._bh3_password = self.bh3_password.text()

        if not self._bh3_account or not self._bh3_password:
            gui_log("请输入账号和密码", LogLevel.WARN)
            QMessageBox.warning(self, "错误", "请输入账号和密码")
            return

        self.bh3_login_btn.setEnabled(False)
        self.bh3_login_btn.setText("登录中...")

        def do_login():
            from api import BSGameSDK
            sdk = BSGameSDK()
            code, message, data = sdk.login(self._bh3_account, self._bh3_password)

            if code == 0 and data.get("access_key"):
                self.bh3_login_result.emit(True, "", str(data["uid"]), data["access_key"], "BiliBili")
            else:
                self.bh3_login_result.emit(False, f"登录失败 (code={code}): {message}", "", "", "")

        threading.Thread(target=do_login, daemon=True).start()

    def _on_bh3_login_result(self, success: bool, msg: str, uid: str, token: str, server: str):
        """B站登录结果处理（在主线程）"""
        self.bh3_login_btn.setText("登录")
        self.bh3_login_btn.setEnabled(True)

        if success:
            gui_log(f"B站登录成功！UID: {uid}")
            QMessageBox.information(self, "成功", f"登录成功！\nUID: {uid}")
            self.login_success.emit(uid, token, uid, "", server)
            self.close()
        else:
            gui_log(f"B站登录失败: {msg}", LogLevel.WARN)
            QMessageBox.warning(self, "错误", msg)

    def closeEvent(self, event):
        """窗口关闭"""
        self._cleanup_qr_thread()
        event.accept()
