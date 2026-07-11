import os
import sys
import threading
import requests
from urllib.parse import urlparse, parse_qs
from typing import Optional
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QComboBox, QLineEdit,
    QMenuBar, QMenu, QMessageBox, QAbstractItemView,
    QInputDialog, QApplication, QGridLayout, QStyledItemDelegate, QProgressDialog,
    QStyle, QStyleOptionViewItem, QDialog, QTextEdit
)
from PyQt6.QtCore import Qt, pyqtSlot, QTimer, QRegularExpression, QModelIndex, QThread, QObject, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QIcon, QCursor, QRegularExpressionValidator, QBrush, QColor, QPixmap, QPainter, QPen

from scanner import ScreenScanner, StreamScanner, LivePlatform, get_live_info, LiveStreamStatus, LiveDouyin, LiveBili
from .account_manager import AccountManager
from core.config import ConfigManager, Account, get_base_dir
from .config_editor import ConfigEditor
from .cookie_refresh_dialog import CookieRefreshDialog
from api import MhyApi, GameType, ServerType, ScanRet, LoginQRCodeState
from .login_window import LoginWindow
from utils.update import restart_program
from core.logger import scanner_log, qr_log, main_log, poll_log, bili_log, gui_log, debug, info, warn, error, LogLevel
from core.logger import Logger

class QRCodePollingWorker(QObject):
    """二维码轮询工作线程"""
    
    # 信号定义
    poll_update = pyqtSignal(int, str, str)  # (attempt, state_name, uid)
    qr_scanned = pyqtSignal(str, str, str)  # (uid, token, ticket) — 扫码成功，等待确认
    qr_confirmed = pyqtSignal(str, str, str)  # (uid, token, ticket) — 确认成功
    qr_expired = pyqtSignal()
    qr_timeout = pyqtSignal()
    error = pyqtSignal(str)
    
    def __init__(self, api: MhyApi, ticket: str, app_id: int, biz_key: str = "", parent=None):
        super().__init__(parent)
        self.api = api
        self.ticket = ticket
        self.app_id = app_id
        self.biz_key = biz_key
        self._running = True
        self._has_emitted_scanned = False  # 避免重复发射 Scanned
    
    def start(self):
        """在工作线程中运行"""
        self._running = True
        self._run()
    
    def stop(self):
        """停止轮询"""
        self._running = False
    
    def _run(self):
        """执行轮询"""
        # 二维码有效期约5分钟，每3秒检查一次（降低频率避免 miHoYo WAF -3503 风控）
        max_attempts = 100  # 轮询次数（约5分钟）
        check_interval = 3  # 每3秒检查一次

        poll_log(f"开始轮询: ticket={self.ticket}, app_id={self.app_id}, biz_key={self.biz_key}, 最多{max_attempts}次({max_attempts*check_interval}秒)")

        consecutive_errors = 0  # 连续错误计数，用于区分瞬时错误和真正的过期

        for i in range(max_attempts):
            if not self._running:
                poll_log("轮询被停止")
                return

            try:
                # 查询状态
                if self.app_id == 1:
                    state, uid, token = self.api.query_qrcode_state(self.ticket, self.biz_key)
                else:
                    state, uid, token = self.api.query_game_qrcode_state(self.ticket, self.app_id, self.biz_key)

                # 成功请求后重置错误计数
                consecutive_errors = 0

                # 发送状态更新信号
                self.poll_update.emit(i + 1, state.name, uid)

                if state == LoginQRCodeState.Expired:
                    poll_log("二维码已过期")
                    self.qr_expired.emit()
                    return

                if state == LoginQRCodeState.Scanned:
                    # 扫码成功，游戏端显示"请在手机上确认登录"
                    if not self._has_emitted_scanned:
                        self._has_emitted_scanned = True
                        poll_log(f"扫码成功! state=Scanned, uid={uid}")
                        self.qr_scanned.emit(uid, token, self.ticket)
                    # 不 return，继续轮询等待 Confirmed

                if state == LoginQRCodeState.Confirmed:
                    poll_log(f"确认成功! uid={uid}")
                    self.qr_confirmed.emit(uid, token, self.ticket)
                    return

            except Exception as e:
                poll_log(f"查询异常: {e}", LogLevel.WARN)
                consecutive_errors += 1
                # 连续5次以上错误才报告（避免网络抖动导致提前终止）
                if consecutive_errors >= 5:
                    self.error.emit(f"连续{consecutive_errors}次查询失败: {str(e)}")
                    # 不终止轮询，继续等待用户操作
                    consecutive_errors = 0  # 重置后继续

            # 等待（最后一次不需要等待），加入微小随机抖动避免风控
            if i < max_attempts - 1:
                import random as _random
                jitter = _random.randint(0, 500)  # 0~500ms
                QThread.msleep(check_interval * 1000 + jitter)

        poll_log("等待超时（轮询达到最大次数）")
        self.qr_timeout.emit()


class DownloadUpdateWorker(QObject):
    """下载更新工作线程（避免 UI 卡死）"""
    progress = pyqtSignal(int)          # 下载进度 0-100
    finished = pyqtSignal(bool, str)    # (success, error_msg)

    def __init__(self, download_url: str):
        super().__init__()
        self.download_url = download_url

    def run(self):
        from utils.update import get_update_manager
        mgr = get_update_manager()
        try:
            success = mgr.download_and_apply_update(
                self.download_url,
                progress_callback=lambda pct: self.progress.emit(pct)
            )
            self.finished.emit(success, "")
        except Exception as e:
            self.finished.emit(False, str(e))


class GameComboDelegate(QStyledItemDelegate):
    """游戏类型下拉选择委托"""

    games = [
        ("崩坏3", GameType.Honkai3),
        ("原神", GameType.Genshin),
        ("星穹铁道", GameType.HonkaiStarRail),
        ("绝区零", GameType.ZenlessZoneZero),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)

    def createEditor(self, parent, option, index):
        editor = QComboBox(parent)
        for name, _ in self.games:
            editor.addItem(name)
        return editor

    def setEditorData(self, editor, index):
        current_text = index.model().data(index, Qt.ItemDataRole.EditRole)
        for i, (name, _) in enumerate(self.games):
            if name == current_text:
                editor.setCurrentIndex(i)
                break

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)


class MainWindow(QMainWindow):
    """主窗口"""
    
    GITHUB_URL = "https://github.com/MR-LIYA/MHY_Scanner"
    GITHUB_ISSUES_URL = "https://github.com/MR-LIYA/MHY_Scanner/issues"

    GAME_NAMES = {
        GameType.Honkai3: "崩坏3",
        GameType.Honkai3_BiliBili: "BiliBili崩坏3",
        GameType.Genshin: "原神",
        GameType.HonkaiStarRail: "星穹铁道",
        GameType.ZenlessZoneZero: "绝区零",
    }

    MID_HELP_TEXT = (
        "当前账号缺少 MID，无法获取登录凭证！\n\n"
        "请输入 MID 后再试。获取 MID 的方法：\n"
        "1. 浏览器打开 https://user.mihoyo.com\n"
        "2. 登录后，地址栏中 ?login_ticket=... 之后的数字串就是 MID\n"
        "3. 也可以尝试 https://api-takumi.mihoyo.com/account/wapi/getUserInfo?stoken=你的stoken\n"
        "（响应中的 data.user_info.aid 即为 MID）\n\n"
        "添加 MID：右键账号 → 编辑MID"
    )

    CHANGELOG_HTML = """
            <h3>v1.0.3 (2026-05)</h3>
            <ul>
                <li>支持手动刷新cookie（抖音直接刷新即可，B站需要扫码登陆一下提取登录态）</li>
                <li>当前版本验证码暂不支持HarmonyOS及IOS，后续修复</li>
            </ul>
            <hr>
            <p style='color:gray;font-size:12px;'>本项目为参考项目改进而来，修复了已知 BUG。</p>
        """

    @staticmethod
    def _create_default_pixmap() -> QPixmap:
        """使用Qt内置图标"""
        style = QApplication.instance().style()
        icon = style.standardIcon(QStyle.StandardPixmap.SP_TitleBarMenuButton)
        return icon.pixmap(64, 64)

    @staticmethod
    def _create_default_icon() -> QIcon:
        """创建默认图标"""
        return QIcon(MainWindow._create_default_pixmap())

    def _load_window_icon(self):
        """加载窗口图标（仅本地加载）"""
        icon_path = str(get_base_dir() / "icons" / "app.png")

        # 本地图标存在则直接使用
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
            return

        # 本地不存在，使用默认图标
        self.setWindowIcon(self._create_default_icon())

    def __init__(self):
        super().__init__()

        # 设置窗口图标（优先网络，本地备用）
        self._load_window_icon()

        # 初始化组件
        self.account_manager = AccountManager()
        self.config = ConfigManager()
        self.api = MhyApi()

        # 应用保存的日志输出模式
        Logger.set_output_mode(self.config.config.log_output_mode)
        # 应用保存的日志等级
        Logger.set_log_level(self.config.config.log_level)
        
        self.setWindowFlags(
            Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.MSWindowsFixedSizeDialogHint # 固定窗口大小
        )
        
        # 扫描器
        self.screen_scanner: Optional[ScreenScanner] = None
        self.stream_scanner: Optional[StreamScanner] = None
        self.current_stream_url = ""
        
        # 登录轮询线程
        self._polling_thread: Optional[QThread] = None
        self._polling_worker: Optional[QRCodePollingWorker] = None
        self._polling_app_id: int = 1  # 保存当前的 app_id
        
        # UI状态
        self.is_screen_scanning = False
        self.is_stream_scanning = False
        self.selected_account: Optional[Account] = None
        self._hovered_row = -1  # 悬浮高亮行
        self.should_restart = False  # 是否需要重启

        # 初始化UI
        self.init_ui()
        
        # 加载账号
        self.load_accounts()
        
        # 自动开始检查
        if self.config.config.auto_start:
            QTimer.singleShot(500, self.start_screen_scan)
    
    def init_ui(self):
        """初始化UI"""
        from PyQt6.QtWidgets import QApplication
        self.setWindowTitle(QApplication.instance().applicationName())
        self.setMinimumSize(500, 600)
        self.resize(500, 650)
        
        # 创建菜单栏
        self.create_menu_bar()
        
        # 中央部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # 主布局
        main_layout = QVBoxLayout()
        central_widget.setLayout(main_layout)
        
        # 状态栏：左侧账号信息，右侧监视状态
        status_layout = QHBoxLayout()
        self.account_label = QLabel("当前选中账号：无")
        self.account_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        status_layout.addWidget(self.account_label)
        status_layout.addStretch()
        self.scan_status_label = QLabel("")
        self.scan_status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        status_layout.addWidget(self.scan_status_label)
        main_layout.addLayout(status_layout)
        
        # 直播间设置区域
        stream_layout = QGridLayout()
        
        # 平台选择
        stream_layout.addWidget(QLabel("直播平台:"), 0, 0)
        self.platform_combo = QComboBox()
        self.platform_combo.addItems(["抖音", "BiliBili"])
        self.platform_combo.setCurrentIndex(self.config.config.last_platform)
        self.platform_combo.currentIndexChanged.connect(self.on_platform_changed)
        stream_layout.addWidget(self.platform_combo, 0, 1, 1, 2)

        # 直播间ID
        stream_layout.addWidget(QLabel("直播间ID:"), 1, 0)
        self.room_id_edit = QLineEdit()
        self.room_id_edit.setPlaceholderText("输入直播间ID（最多12位数字）")
        self.room_id_edit.setMaxLength(12)  # 限制最多12位数字
        # 使用正则表达式验证器：只允许1-12位数字
        room_id_validator = QRegularExpressionValidator(QRegularExpression("^[0-9]{1,12}$"), self)
        self.room_id_edit.setValidator(room_id_validator)
        self.room_id_edit.textChanged.connect(self.on_room_id_changed)  # 文本变化时保存
        stream_layout.addWidget(self.room_id_edit, 1, 1, 1, 2)
        # 初始化时加载对应平台的直播间ID
        self.room_id_edit.setText(self.config.config.get_last_room_id(self.config.config.last_platform))
        
        main_layout.addLayout(stream_layout)
        
        # 账号表格
        # ===== 账号表格设置 =====
        # 显示设置
        SHOW_GRID = False          # False隐藏网格线，True显示网格线
        SELECTION_COLOR = "#CACACA"  # 选中行背景色
        # ========================

        
        self.account_table = QTableWidget()
        self.account_table.setColumnCount(5)
        self.account_table.setHorizontalHeaderLabels(["", "UID", "用户名", "服务器", "备注"])
        self.account_table.setShowGrid(SHOW_GRID)
        self.account_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.account_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.account_table.setColumnWidth(0, 18)
        self.account_table.horizontalHeader().setStyleSheet(
            "QHeaderView::section:first { border-right: none; }"
        )
        self.account_table.verticalHeader().hide()  # 隐藏行号
        self.account_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.account_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.account_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.account_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.account_table.setStyleSheet(f"""
            QTableWidget::item:hover {{
                background-color: #E8E8E8;
                color: #000000;
            }}
            QTableWidget::item:selected {{
                background-color: {SELECTION_COLOR};
                color: #000000;
            }}
            QTableWidget::item:selected:!active {{
                background-color: #D0D0D0;
            }}
        """)
        self.account_table.itemDoubleClicked.connect(self.on_table_double_clicked)
        self.account_table.itemChanged.connect(self.on_table_item_changed)  # 监听修改保存备注
        self.account_table.itemClicked.connect(self.on_table_clicked)  # 单击也选中
        self.account_table.viewport().setMouseTracking(True)
        self.account_table.viewport().installEventFilter(self)  # 安装事件过滤器用于悬浮高亮
        self.account_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.account_table.customContextMenuRequested.connect(self.on_context_menu)
        main_layout.addWidget(self.account_table)
        
        # 控制按钮区域
        button_layout = QHBoxLayout()
        
        self.btn_screen_scan = QPushButton("监视屏幕")
        self.btn_screen_scan.clicked.connect(self.toggle_screen_scan)
        button_layout.addWidget(self.btn_screen_scan)
        
        self.btn_stream_scan = QPushButton("监视直播间")
        self.btn_stream_scan.clicked.connect(self.toggle_stream_scan)
        button_layout.addWidget(self.btn_stream_scan)
        
        main_layout.addLayout(button_layout)
        
        # 版本信息（右下角显示）
        bottom_layout = QHBoxLayout()
        bottom_layout.addStretch()
        version_label = QLabel(f"版本 {QApplication.instance().applicationVersion()}")
        version_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bottom_layout.addWidget(version_label)
        main_layout.addLayout(bottom_layout)
    
    @staticmethod
    def _make_level_icon(r: int, g: int, b: int) -> QIcon:
        """生成 12×12 的纯色圆点图标"""
        px = QPixmap(12, 12)
        px.fill(Qt.GlobalColor.transparent)
        painter = QPainter(px)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(r, g, b))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(2, 2, 8, 8)
        painter.end()
        return QIcon(px)

    def create_menu_bar(self):
        """创建菜单栏"""
        menubar = self.menuBar()
        
        # 账号管理菜单
        account_menu = menubar.addMenu("账号管理")
        
        add_account_action = QAction("添加账号", self)
        add_account_action.triggered.connect(self.show_add_account_dialog)
        account_menu.addAction(add_account_action)
        
        delete_account_action = QAction("删除账号", self)
        delete_account_action.triggered.connect(self.delete_selected_account)
        account_menu.addAction(delete_account_action)
        
        account_menu.addSeparator()
        
        self.set_default_action = QAction("设置为默认账号", self)
        self.set_default_action.triggered.connect(self.set_default_account)
        account_menu.addAction(self.set_default_action)
        
        # 设置菜单
        settings_menu = menubar.addMenu("设置")
        
        self.action_stay_on_top = QAction("窗口置顶", self, checkable=True)
        self.action_stay_on_top.triggered.connect(self.toggle_stay_on_top)
        settings_menu.addAction(self.action_stay_on_top)

        settings_menu.addSeparator()

        # 日志输出子菜单（checkable QActionGroup，互斥单选）
        # 打包后仅输出到文件，隐藏切换选项
        if not getattr(sys, "frozen", False):
            log_output_menu = settings_menu.addMenu("日志输出")
            log_output_group = QActionGroup(self)
            log_output_group.setExclusive(True)

            self.action_log_console = QAction("控制台", self, checkable=True)
            self.action_log_file = QAction("文件", self, checkable=True)
            log_output_group.addAction(self.action_log_console)
            log_output_group.addAction(self.action_log_file)
            log_output_menu.addActions([self.action_log_console, self.action_log_file])

            # 恢复上次选择
            self._update_log_output_menu(self.config.config.log_output_mode)

            self.action_log_console.triggered.connect(lambda: self.on_log_output_menu_changed("console"))
            self.action_log_file.triggered.connect(lambda: self.on_log_output_menu_changed("file"))

        # 日志等级子菜单（checkable QActionGroup，互斥单选）
        log_level_menu = settings_menu.addMenu("日志等级")
        log_level_group = QActionGroup(self)
        log_level_group.setExclusive(True)

        self.action_log_debug = QAction("DEBUG", self, checkable=True)
        self.action_log_debug.setIcon(self._make_level_icon(158, 158, 158))   # 灰色
        self.action_log_info = QAction("INFO", self, checkable=True)
        self.action_log_info.setIcon(self._make_level_icon(33, 150, 243))     # 蓝色
        self.action_log_warn = QAction("WARN", self, checkable=True)
        self.action_log_warn.setIcon(self._make_level_icon(255, 152, 0))      # 橙色
        self.action_log_error = QAction("ERROR", self, checkable=True)
        self.action_log_error.setIcon(self._make_level_icon(244, 67, 54))     # 红色
        log_level_group.addAction(self.action_log_debug)
        log_level_group.addAction(self.action_log_info)
        log_level_group.addAction(self.action_log_warn)
        log_level_group.addAction(self.action_log_error)
        log_level_menu.addActions([self.action_log_debug, self.action_log_info, self.action_log_warn, self.action_log_error])

        # 恢复上次选择
        self._update_log_level_menu(self.config.config.log_level)

        self.action_log_debug.triggered.connect(lambda: self.on_log_level_menu_changed(0))
        self.action_log_info.triggered.connect(lambda: self.on_log_level_menu_changed(1))
        self.action_log_warn.triggered.connect(lambda: self.on_log_level_menu_changed(2))
        self.action_log_error.triggered.connect(lambda: self.on_log_level_menu_changed(3))

        settings_menu.addSeparator()

        # 三个功能开关（checkable QAction）
        self.action_auto_confirm = QAction("自动二次确认", self, checkable=True)
        self.action_auto_confirm.setChecked(self.config.config.auto_login)
        self.action_auto_confirm.toggled.connect(self.on_auto_confirm_changed)
        settings_menu.addAction(self.action_auto_confirm)

        self.action_auto_start = QAction("启动时自动监视屏幕", self, checkable=True)
        self.action_auto_start.setChecked(self.config.config.auto_start)
        self.action_auto_start.toggled.connect(self.on_auto_start_changed)
        settings_menu.addAction(self.action_auto_start)

        self.action_auto_exit = QAction("扫码成功后自动退出", self, checkable=True)
        self.action_auto_exit.setChecked(self.config.config.auto_exit)
        self.action_auto_exit.toggled.connect(self.on_auto_exit_changed)
        settings_menu.addAction(self.action_auto_exit)

        settings_menu.addSeparator()
        
        open_config_action = QAction("打开配置文件", self)
        open_config_action.triggered.connect(self.open_config_file)
        settings_menu.addAction(open_config_action)

        refresh_cookie_action = QAction("刷新Cookie", self)
        refresh_cookie_action.triggered.connect(self.show_cookie_refresh_dialog)
        settings_menu.addAction(refresh_cookie_action)
        
        check_update_action = QAction("检查更新", self)
        check_update_action.triggered.connect(self.check_for_updates)
        settings_menu.addAction(check_update_action)
        
        # 帮助菜单
        help_menu = menubar.addMenu("帮助")
        
        about_action = QAction("关于", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

        changelog_action = QAction("更新日志", self)
        changelog_action.triggered.connect(self.show_changelog)
        help_menu.addAction(changelog_action)
        
        homepage_action = QAction("项目主页", self)
        homepage_action.triggered.connect(self.open_homepage)
        help_menu.addAction(homepage_action)
    
        feedback_action = QAction("反馈问题", self)
        feedback_action.triggered.connect(self.open_github_issues)
        help_menu.addAction(feedback_action)
        
    def load_accounts(self):
        """加载账号到表格"""
        # 暂时断开 itemChanged 信号，避免 setItem() 填充表格时触发保存逻辑
        self.account_table.blockSignals(True)
        self.account_table.setRowCount(0)

        for account in self.account_manager.get_accounts():
            row = self.account_table.rowCount()
            self.account_table.insertRow(row)

            # 检查是否为默认账号
            is_default = self.config.is_default_account(account.uid, account.server_type)
            name = account.name

            # 设置单元格内容
            dot_item = QTableWidgetItem("●" if is_default else "")
            uid_item = QTableWidgetItem(account.uid)
            name_item = QTableWidgetItem(name)
            server_item = QTableWidgetItem(
                self.account_manager.get_server_name(ServerType(account.server_type))
            )
            notes_item = QTableWidgetItem(account.notes)

            # 黑点列居中，其余列对齐设置
            dot_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter)
            uid_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter)
            name_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter)
            server_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter)
            notes_item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)

            self.account_table.setItem(row, 0, dot_item)
            self.account_table.setItem(row, 1, uid_item)
            self.account_table.setItem(row, 2, name_item)
            self.account_table.setItem(row, 3, server_item)
            self.account_table.setItem(row, 4, notes_item)

        # 恢复信号
        self.account_table.blockSignals(False)

        # 加载完成后自动选中默认账号
        self.select_default_account()
    
    def select_default_account(self):
        """选中默认账号"""
        default_uid, default_server_type = self.config.get_default_account()
        if not default_uid:
            return
        
        # 遍历表格找到默认账号（判断逻辑与 get_server_type_from_table 保持一致）
        for row in range(self.account_table.rowCount()):
            uid_item = self.account_table.item(row, 1)
            server_text = self.account_table.item(row, 3).text()
            if uid_item and server_text:
                uid = uid_item.text()
                server_type = int(ServerType.Official) if "BiliBili" not in server_text else int(ServerType.BiliBili)
                if uid == default_uid and server_type == default_server_type:
                    self.select_account_by_row(row)
                    return
    
    # ========== 槽函数 ==========
    
    @pyqtSlot()
    def on_platform_changed(self):
        """平台改变"""
        platform = self.platform_combo.currentIndex()
        self.config.update_last_platform(platform)
        # 加载对应平台的直播间ID（阻断信号，避免触发 textChanged → 回写导致覆盖）
        self.room_id_edit.blockSignals(True)
        self.room_id_edit.setText(self.config.config.get_last_room_id(platform))
        self.room_id_edit.blockSignals(False)
    
    @pyqtSlot(str)
    def on_room_id_changed(self, room_id: str):
        """直播间ID改变时保存"""
        # 保存全局的直播间ID
        self.config.update_last_room_id(room_id)
        # 如果有选中账号，也保存到该账号
        if self.selected_account:
            self.config.update_account_room_id(
                self.selected_account.uid,
                self.selected_account.server_type,
                room_id
            )
    
    @pyqtSlot(bool)
    def on_auto_start_changed(self, checked: bool):
        """自动开始改变"""
        self.config.update_auto_start(checked)
    
    @pyqtSlot(bool)
    def on_auto_exit_changed(self, checked: bool):
        """自动退出改变"""
        self.config.update_auto_exit(checked)

    @pyqtSlot(bool)
    def on_auto_confirm_changed(self, checked: bool):
        """自动二次确认改变"""
        self.config.update_auto_login(checked)

    def _update_log_output_menu(self, mode: str):
        """更新日志输出菜单勾选状态（非 frozen 模式才存在菜单项）"""
        if not hasattr(self, "action_log_console"):
            return
        self.action_log_console.setChecked(mode == "console")
        self.action_log_file.setChecked(mode == "file")

    def on_log_output_menu_changed(self, mode: str):
        """日志输出模式改变（设置菜单触发）
        
        使用 QTimer 延迟执行，避免菜单关闭期间修改 QAction 文本导致 Qt 内部冲突崩溃。
        """
        QTimer.singleShot(0, lambda: self._apply_log_output_mode(mode))

    def _apply_log_output_mode(self, mode: str):
        """实际应用日志输出模式"""
        self._update_log_output_menu(mode)
        Logger.set_output_mode(mode)
        self.config.update_log_output_mode(mode)

    def _update_log_level_menu(self, level: int):
        """更新日志等级菜单勾选状态"""
        self.action_log_debug.setChecked(level == 0)
        self.action_log_info.setChecked(level == 1)
        self.action_log_warn.setChecked(level == 2)
        self.action_log_error.setChecked(level == 3)

    def on_log_level_menu_changed(self, level: int):
        """日志等级改变（设置菜单触发）"""
        QTimer.singleShot(0, lambda: self._apply_log_level(level))

    def _apply_log_level(self, level: int):
        """实际应用日志等级"""
        self._update_log_level_menu(level)
        Logger.set_log_level(level)
        self.config.update_log_level(level)
    
    @pyqtSlot(QTableWidgetItem)
    def on_table_clicked(self, item: QTableWidgetItem):
        """表格单击选中账号"""
        self.select_account_by_row(item.row())

    def _update_hover_row(self, row: int):
        """更新悬浮高亮行"""
        if row == self._hovered_row:
            return

        # 阻断信号：setBackground 会触发 itemChanged，避免误保存
        self.account_table.blockSignals(True)

        # 清除旧的高亮行（恢复默认背景）
        if self._hovered_row >= 0 and self._hovered_row < self.account_table.rowCount():
            for col in range(self.account_table.columnCount()):
                item = self.account_table.item(self._hovered_row, col)
                if item:
                    item.setBackground(QBrush())  # 空QBrush表示默认背景

        # 设置新的高亮行
        if row >= 0:
            for col in range(self.account_table.columnCount()):
                item = self.account_table.item(row, col)
                if item:
                    item.setBackground(QBrush(QColor("#E8E8E8")))

        self.account_table.blockSignals(False)
        self._hovered_row = row

    def on_table_hover(self, index: QModelIndex):
        """表格悬浮时整行高亮"""
        if index.isValid():
            self._update_hover_row(index.row())

    @pyqtSlot(QTableWidgetItem)
    def on_table_double_clicked(self, item: QTableWidgetItem):
        """表格双击编辑（备注列可编辑）"""
        row = item.row()
        if item.column() == 4:  # 备注列
            self.account_table.editItem(item)
        else:
            # 选中账号
            self.select_account_by_row(row)

    @pyqtSlot(QTableWidgetItem)
    def on_table_item_changed(self, item: QTableWidgetItem):
        """表格项目修改后保存"""
        row = item.row()
        # 获取该行的账号信息
        uid_item = self.account_table.item(row, 1)
        server_item = self.account_table.item(row, 3)
        if not (uid_item and server_item):
            return

        uid = uid_item.text()
        server_type = 1 if server_item.text() == "官服" else 2

        if item.column() == 4:  # 备注列
            notes = item.text()
            self.account_manager.update_notes(uid, server_type, notes)
            main_log(f"备注已保存: uid={uid}, server={server_type}, notes={notes}")

        # 恢复禁用编辑触发器
        self.account_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    
    @pyqtSlot()
    def on_context_menu(self):
        """右键菜单"""
        menu = QMenu()
        
        # 获取当前选中的行
        current_row = self.account_table.currentRow()
        if current_row < 0:
            current_row = 0
        
        uid_item = self.account_table.item(current_row, 1)
        
        add_action = menu.addAction("添加账号")
        add_action.triggered.connect(self.show_add_account_dialog)
        
        if uid_item:
            # 先选中该行
            self.select_account_by_row(current_row)
            
            if self.selected_account:
                # 编辑MID
                edit_mid_action = menu.addAction("编辑MID")
                edit_mid_action.triggered.connect(self.edit_account_mid)
                
                menu.addSeparator()
                
                # 检查当前选中账号是否为默认账号
                is_default = self.config.is_default_account(
                    self.selected_account.uid, 
                    self.selected_account.server_type
                )
                has_default = bool(self.config.get_default_account()[0])
                
                if is_default:
                    unset_action = menu.addAction("取消设置为默认账号")
                    unset_action.triggered.connect(self.clear_default)
                else:
                    text = "替换为默认账号" if has_default else "设置为默认账号"
                    set_default_action = menu.addAction(text)
                    set_default_action.triggered.connect(self.set_as_default)
                
                delete_action = menu.addAction("删除账号")
                delete_action.triggered.connect(self.delete_selected_account)
        
        menu.exec(QCursor.pos())
    
    def set_as_default(self):
        """设置为默认账号"""
        if self.selected_account:
            self.config.set_default_account(
                self.selected_account.uid,
                self.selected_account.server_type
            )
            # 刷新表格显示
            self.load_accounts()
            msg = f"已将「{self.selected_account.name}」设置为默认账号"
            gui_log(msg)
            QMessageBox.information(self, "成功", msg)

    def clear_default(self):
        """取消默认账号"""
        self.config.clear_default_account()
        # 刷新表格显示
        self.load_accounts()
        msg = "已取消默认账号"
        gui_log(msg)
        QMessageBox.information(self, "成功", msg)

    def edit_account_mid(self):
        """编辑账号的MID"""
        if not self.selected_account:
            return
        
        old_mid = self.selected_account.mid or ""
        mid, ok = QInputDialog.getText(
            self, "编辑MID",
            f"为账号「{self.selected_account.name}」输入MID：",
            QLineEdit.EchoMode.Normal,
            old_mid
        )
        if ok and mid.strip():
            self.account_manager.update_mid(
                self.selected_account.uid,
                self.selected_account.server_type,
                mid.strip()
            )
            self.load_accounts()
            QMessageBox.information(self, "成功", f"已更新账号「{self.selected_account.name}」的MID")
            gui_log(f"更新账号MID: {self.selected_account.name} -> {mid.strip()}")
    
    @pyqtSlot(str, int, int)
    def on_qrcode_detected(self, qr_data: str, game_type_val: int, app_id: int):
        """
        检测到游戏二维码（来自 ScreenScanner/StreamScanner 的 qrcode_game_detected 信号）
        此信号已经在扫描器中经过 C++ 风格的验证（URL长度>=85 + offset 79 匹配游戏类型）

        C++ 对应：QRCodeForScreen::LoginOfficial / QRCodeForStream::LoginOfficial
        官方服流程：PandaScanQRCode(scanUrl,ticket,gameType) 获取 passport 二维码
                  → ScanPassportQRLogin + ConfirmPassportQRLogin（使用已登录账号的 stoken/mid）
        B服(崩坏3)流程：scanCheck + scanConfirm
        """
        qr_log(f"检测到二维码: 类型={GameType(game_type_val).name}, app_id={app_id}")

        # 对齐 C++ parseOfficialQRCode：ticket 优先，否则 tk，或取末尾24字符
        ticket = self._extract_ticket_from_qr(qr_data)
        if not ticket:
            qr_log("无法提取ticket", LogLevel.WARN)
            return
        qr_log(f"Ticket: {ticket}, app_id: {app_id}")

        if not self.selected_account:
            gui_log("请先选择一个账号", LogLevel.WARN)
            QMessageBox.warning(self, "提示", "请先选择一个账号")
            return

        # 对齐 C++ QRCodeForScreen/Stream：按服务器类型分流
        if self.selected_account.server_type == int(ServerType.BiliBili):
            # 崩坏3 B服：scanCheck + scanConfirm
            self.process_bilibili_login(ticket, qr_data)
        else:
            # 官服：PandaScan + passport 扫码/确认
            self.process_official_screen_login(ticket, app_id)

    @staticmethod
    def _extract_ticket_from_qr(qr_data: str) -> str:
        """从二维码URL提取 ticket（对齐 C++ parseOfficialQRCode: ticket → tk → 末尾24字符）"""
        try:
            parsed = urlparse(qr_data)
            params = parse_qs(parsed.query)
            t = params.get("ticket", [""])[0] or params.get("tk", [""])[0]
            if t:
                return t
        except Exception:
            pass
        return qr_data[-24:] if len(qr_data) >= 24 else ""

    def process_official_screen_login(self, ticket: str, app_id: int):
        """官服屏幕/直播扫码登录（对齐 C++ LoginOfficial → PandaScanQRCode → continueLastLogin）

        1. PandaScanQRCode：向游戏内二维码 scan 端点发送扫码请求，获取 passport 二维码 URL
        2. 自动确认开启则直接继续；否则弹出确认对话框
        3. ScanPassportQRLogin + ConfirmPassportQRLogin：使用已登录账号的 stoken/mid 完成授权
        """
        account = self.selected_account

        # 对齐 C++ pBtstartScreen：开扫/使用前先校验 stoken 是否有效，
        # 失效则直接中止（对应 C++ CheckStokenValid → "登录状态失效，请重新添加账号！"）
        if not self.api.check_stoken_valid(account.token, account.mid):
            gui_log("账号登录状态失效（stoken 校验失败）", LogLevel.ERROR)
            QMessageBox.warning(self, "提示", "登录状态失效，\n请重新添加账号！")
            return

        scan_url = MhyApi.get_qrcode_scan_url(app_id)
        qr_log(f"PandaScanQRCode: scan_url={scan_url}, app_id={app_id}", LogLevel.DEBUG)

        passport_url = self.api.panda_scan_qrcode(scan_url, ticket, app_id)
        if not passport_url:
            gui_log("扫码确认失败！", LogLevel.WARN)
            QMessageBox.warning(self, "提示", "扫码确认失败！")
            return

        # 停止扫描（与 C++ 检测到后即 stop() 一致）
        if self.is_screen_scanning:
            self.toggle_screen_scan()
        if self.is_stream_scanning:
            self.toggle_stream_scan()

        # 手动确认：弹出对话框让用户确认（自动确认则跳过）
        if not self.config.config.auto_login:
            self._set_window_to_front()
            game_name = self.GAME_NAMES.get(GameType(app_id), "未知游戏")
            info = f"正在使用账号 {account.name}\n登录 {game_name}\n\n确认登录？"
            reply = QMessageBox.question(
                self, "登录确认", info,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                qr_log("用户取消确认")
                return

        # 对齐 C++ continueLastLogin：需要 mid 才能将 stoken 用于 passport 授权
        if not account.mid:
            qr_log("账号缺少 mid，无法完成 passport 扫码", LogLevel.WARN)
            QMessageBox.warning(self, "错误", self.MID_HELP_TEXT)
            return

        ok = self.api.scan_passport_qr_login(passport_url, account.token, account.mid)
        if not ok:
            gui_log("扫码二次确认失败！", LogLevel.ERROR)
            QMessageBox.warning(self, "提示", "扫码二次确认失败！")
            return

        ok = self.api.confirm_passport_qr_login(passport_url, account.token, account.mid)
        if ok:
            gui_log("扫码登录成功！")
            QMessageBox.information(self, "成功", "扫码登录成功！")
            if self.config.config.auto_exit:
                self.close()
        else:
            gui_log("扫码确认失败！", LogLevel.ERROR)
            QMessageBox.warning(self, "失败", "扫码确认失败！")
    
    @pyqtSlot(bool)
    def on_scan_finished(self, success: bool):
        """扫描完成"""
        scanner_log(f"扫描结束: {'成功' if success else '失败'}")
    
    @pyqtSlot(str)
    def on_scan_error(self, error: str):
        """扫描错误"""
        scanner_log(f"扫描错误: {error}", LogLevel.WARN)
        gui_log(f"扫描错误: {error}", LogLevel.WARN)
        QMessageBox.warning(self, "扫描错误", error)
    
    # ========== 功能函数 ==========
    
    def toggle_stay_on_top(self, checked: bool):
        """切换窗口置顶状态"""
        if checked:
            self.setWindowFlags(
                self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint
            )
            main_log("窗口已置顶")
        else:
            self.setWindowFlags(
                self.windowFlags() & ~Qt.WindowType.WindowStaysOnTopHint
            )
            main_log("窗口已取消置顶")
        self.show()
    
    def select_account_by_row(self, row: int):
        """通过行号选中账号"""
        if row < 0 or row >= self.account_table.rowCount():
            return
        
        # 设置整行选中
        self.account_table.selectRow(row)
        
        uid = self.account_table.item(row, 1).text()
        server_type = self.get_server_type_from_table(row)
        
        self.selected_account = self.account_manager.get_account(uid, server_type)
        
        if self.selected_account:
            self.account_label.setText(f"当前选中账号：{self.selected_account.name}")
            self.api.set_server_type(ServerType(self.selected_account.server_type))
            self.api.set_game_type(GameType(self.selected_account.game_type))
            main_log(f"选中账号: {self.selected_account.name}, uid={uid}, server={server_type}, game={self.selected_account.game_type}")
            # 填充该账号上次使用的直播间ID，没有则保留当前平台已设置的ID（避免清空覆盖）
            if self.selected_account.last_room_id:
                self.room_id_edit.blockSignals(True)
                self.room_id_edit.setText(self.selected_account.last_room_id)
                self.room_id_edit.blockSignals(False)
        
        # 更新菜单栏"默认账号"按钮文字
        self._update_default_action_text()
    
    def _update_default_action_text(self):
        """根据当前选中账号更新菜单栏和右键菜单文字"""
        if not self.selected_account:
            return
        
        is_default = self.config.is_default_account(
            self.selected_account.uid,
            self.selected_account.server_type
        )
        has_default = bool(self.config.get_default_account()[0])
        
        if is_default:
            self.set_default_action.setText("取消设置为默认账号")
            self.set_default_action.triggered.disconnect()
            self.set_default_action.triggered.connect(self.clear_default)
        else:
            if has_default:
                self.set_default_action.setText("替换为默认账号")
            else:
                self.set_default_action.setText("设置为默认账号")
            self.set_default_action.triggered.disconnect()
            self.set_default_action.triggered.connect(self.set_default_account)
    
    def get_server_type_from_table(self, row: int) -> int:
        """从表格行获取服务器类型"""
        server_text = self.account_table.item(row, 3).text()
        if "BiliBili" in server_text:
            return int(ServerType.BiliBili)
        return int(ServerType.Official)
    
    def process_qr_login(self, ticket: str, qr_data: str, app_id: int = 1):
        """处理二维码登录"""
        if not self.selected_account:
            gui_log("请先选择一个账号", LogLevel.WARN)
            QMessageBox.warning(self, "提示", "请先选择一个账号")
            return

        # 停止扫描
        if self.is_screen_scanning:
            self.toggle_screen_scan()
        if self.is_stream_scanning:
            self.toggle_stream_scan()

        # 从QR码URL中提取biz_key（可能需要用于API请求）
        biz_key = ""
        try:
            parsed = urlparse(qr_data)
            params = parse_qs(parsed.query)
            biz_key = params.get("biz_key", [""])[0]
            qr_log(f"biz_key={biz_key}", LogLevel.DEBUG)
        except Exception as e:
            qr_log(f"解析biz_key失败: {e}", LogLevel.WARN)

        # 根据服务器类型处理登录
        if self.selected_account.server_type == int(ServerType.Official):
            self.process_official_login(ticket, app_id, biz_key)
        else:
            self.process_bilibili_login(ticket, qr_data)
    
    def process_official_login(self, ticket: str, app_id: int = 1, biz_key: str = ""):
        """处理官服登录 - 多线程轮询等待扫码确认"""
        qr_log(f"开始轮询等待扫码: ticket={ticket}, app_id={app_id}, biz_key={biz_key}")

        # 停止之前的轮询（如果有）
        self._stop_polling()

        # 保存 app_id（信号发出时 worker 可能已被清理）
        self._polling_app_id = app_id
        self._polling_biz_key = biz_key

        # 创建新线程和工作对象
        self._polling_thread = QThread()
        self._polling_worker = QRCodePollingWorker(self.api, ticket, app_id, biz_key)
        self._polling_worker.moveToThread(self._polling_thread)

        # 连接信号
        self._polling_thread.started.connect(self._polling_worker.start)
        self._polling_worker.poll_update.connect(self._on_polling_update)
        self._polling_worker.qr_scanned.connect(self._on_qr_scanned)
        self._polling_worker.qr_confirmed.connect(self._on_qr_confirmed)
        self._polling_worker.qr_expired.connect(self._on_qr_expired)
        self._polling_worker.qr_timeout.connect(self._on_qr_timeout)
        self._polling_worker.error.connect(self._on_polling_error)

        # 线程结束时清理
        self._polling_thread.finished.connect(self._on_polling_finished)

        # 启动线程
        self._polling_thread.start()
    
    def _stop_polling(self):
        """停止轮询线程"""
        # 先通知 worker 停止（设置标志位）
        if self._polling_worker:
            self._polling_worker.stop()
        if self._polling_thread:
            self._polling_thread.quit()
            if not self._polling_thread.wait(3000):
                poll_log("线程未在3秒内结束，强制terminate", LogLevel.WARN)
                self._polling_thread.terminate()
                self._polling_thread.wait(1000)
            if not self._polling_thread.isRunning():
                self._polling_thread.deleteLater()
            self._polling_thread = None
            self._polling_worker = None
    
    def _on_polling_update(self, attempt: int, state_name: str, uid: str):
        """轮询状态更新"""
        poll_log(f"第{attempt}次查询: state={state_name}, uid={uid}", LogLevel.DEBUG)
    
    def _on_qr_scanned(self, uid: str, token: str, ticket: str):
        """
        二维码已被扫码（游戏端显示"扫码成功，请在手机上确认登录"）
        如果开启了自动确认，则立即调用 confirm_qrcode 完成登录
        """
        qr_log(f"已扫码: uid={uid}, auto_login={self.config.config.auto_login}")

        # 保存确认信息
        self._confirmed_uid = uid
        self._confirmed_token = token
        self._confirmed_ticket = ticket

        if self.config.config.auto_login:
            qr_log("自动确认已开启，立即确认登录")
            self._do_confirm_login()
        else:
            qr_log("自动确认未开启，等待手机确认...")

    def _on_qr_confirmed(self, uid: str, token: str, ticket: str):
        """
        二维码已确认
        - auto_login=True:  已在 _on_qr_scanned 中调用过 confirm_qrcode，此处直接报告成功
        - auto_login=False: 用户在手机上点击了确认，需要弹窗确认或调用 _do_confirm_login
        """
        qr_log(f"确认成功! uid={uid}")

        # 保存确认信息
        self._confirmed_uid = uid
        self._confirmed_token = token
        self._confirmed_ticket = ticket

        if self.config.config.auto_login:
            # 已在 _on_qr_scanned 中调用过 confirm_qrcode，此处直接报告成功
            qr_log("登录成功!（自动确认已完成）")
            self._stop_polling()
            gui_log("扫码成功！")
            QMessageBox.information(self, "提示", "扫码成功！")
            if self.config.config.auto_exit:
                self.close()
        else:
            # 手动确认：弹出对话框让用户确认
            self._show_login_confirm_dialog()

    def _do_confirm_login(self):
        """执行确认登录（参考C++ continueLastLogin）
        发送 confirm_qrcode 请求后不立即停止轮询，
        让轮询检测到 Confirmed 状态后由 _on_qr_confirmed 统一处理成功/失败。
        """
        uid = getattr(self, '_confirmed_uid', '')
        token = getattr(self, '_confirmed_token', '')
        ticket = getattr(self, '_confirmed_ticket', '')
        app_id = getattr(self, '_polling_app_id', 1)
        biz_key = getattr(self, '_polling_biz_key', '')

        if not uid:
            qr_log("无确认信息", LogLevel.WARN)
            self._stop_polling()
            return

        qr_log(f"调用 confirm_qrcode: ticket={ticket}, uid={uid}, app_id={app_id}", LogLevel.DEBUG)
        success = self.api.confirm_qrcode(ticket, uid, token, app_id, biz_key)
        qr_log(f"confirm_qrcode 返回: {success}", LogLevel.DEBUG)

        if success:
            qr_log("confirm_qrcode 成功，等待服务端确认...")
            # 不停止轮询，让 _on_qr_confirmed 处理后续流程
        else:
            qr_log("confirm_qrcode 失败，停止轮询", LogLevel.WARN)
            self._stop_polling()
            gui_log("扫码二次确认失败！", LogLevel.ERROR)
            QMessageBox.warning(self, "提示", "扫码二次确认失败！")

    def _handle_game_qr_confirm(self, ticket: str, app_id: int, biz_key: str = ""):
        """
        处理游戏内二维码确认（参考C++: scan→直接confirm，无query轮询）
        C++ 对应流程:
          ScanQRLogin(scanUrl, ticket, gameType)
          → if auto_login: continueLastLogin()
          → else: emit loginConfirm → 用户点击 → continueLastLogin()
            → ConfirmQRLogin(confirmUrl, uid, gameToken, ticket, gameType)
        """
        account = self.selected_account
        if not account:
            QMessageBox.warning(self, "提示", "请先选择一个账号")
            return

        # 停止扫描（与 process_qr_login 一致）
        if self.is_screen_scanning:
            self.toggle_screen_scan()
        if self.is_stream_scanning:
            self.toggle_stream_scan()

        # 参考 C++: 如果不是自动登录，弹出确认对话框
        if not self.config.config.auto_login:
            self._set_window_to_front()

            game_name = self.GAME_NAMES.get(GameType(account.game_type), "未知游戏")
            info = f"正在使用账号 {account.name}\n登录 {game_name}\n\n确认登录？"

            reply = QMessageBox.question(
                self, "登录确认", info,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                qr_log("用户取消确认")
                return

        # 参考 C++ GetGameTokenByStoken：通过 stoken 获取 game_token
        # account.token 是 stoken（长票据），必须通过 mid 转换为 game_token（短票据）
        # C++ 中如果获取失败直接 AccountError 并 return
        if not account.mid:
            qr_log("账号缺少 mid，无法将 stoken 转换为 game_token", LogLevel.WARN)
            QMessageBox.warning(self, "错误", self.MID_HELP_TEXT)
            return

        try:
            code, game_token = self.api.get_game_token_by_stoken(account.token, account.mid)
            if code != 0 or not game_token:
                qr_log(f"通过 stoken 获取 game_token 失败，code={code}", LogLevel.WARN)
                QMessageBox.warning(self, "错误", f"登录凭证获取失败（code={code}）！\n请检查账号 token 和 MID 是否有效。")
                return
            qr_log("通过 stoken 获取 game_token 成功")
        except Exception as e:
            qr_log(f"获取 game_token 异常: {e}", LogLevel.ERROR)
            QMessageBox.warning(self, "错误", f"登录凭证获取异常：{e}")
            return

        # 参考 C++ ConfirmQRLogin：scan 后直接 confirm（无 biz_key）
        qr_log(f"confirm_qrcode: ticket={ticket}, uid={account.uid}, app_id={app_id}", LogLevel.DEBUG)
        success = self.api.confirm_qrcode(ticket, account.uid, game_token, app_id)
        qr_log(f"confirm_qrcode 返回: {success}", LogLevel.DEBUG)

        if success:
            gui_log("扫码登录成功！")
            QMessageBox.information(self, "成功", "扫码登录成功！")
            if self.config.config.auto_exit:
                self.close()
        else:
            gui_log("扫码确认失败！", LogLevel.ERROR)
            QMessageBox.warning(self, "失败", "扫码确认失败！")

    def _show_login_confirm_dialog(self):
        """弹出登录确认对话框（参考C++ loginConfirmTip）"""
        if not self.selected_account:
            self._stop_polling()
            return

        # 获取账号信息
        account_name = self.selected_account.name
        game_type = GameType(self.selected_account.game_type)

        game_name = self.GAME_NAMES.get(game_type, "未知游戏")

        info = f"正在使用账号 {account_name}\n登录 {game_name}\n\n确认登录？"

        # 设置窗口到前台
        self._set_window_to_front()

        reply = QMessageBox.question(
            self, "登录确认", info,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        self._stop_polling()

        if reply == QMessageBox.StandardButton.Yes:
            self._do_confirm_login()
        # 如果选 No，就直接停止，不做任何事

    def _set_window_to_front(self):
        """将窗口置顶并激活"""
        self.showNormal()
        self.activateWindow()
        self.raise_()
    
    def _on_qr_expired(self):
        """二维码已过期"""
        poll_log("二维码已过期")
        gui_log("二维码已过期，请重新生成", LogLevel.WARN)
        QMessageBox.warning(self, "登录失败", "二维码已过期，请重新生成")
        self._stop_polling()

    def _on_qr_timeout(self):
        """轮询超时"""
        poll_log("等待超时")
        gui_log("等待扫码超时，请重试。", LogLevel.WARN)
        QMessageBox.warning(self, "登录失败", "等待扫码超时，请重试。")
        self._stop_polling()
    
    def _on_polling_error(self, error_msg: str):
        """轮询错误"""
        poll_log(f"轮询错误: {error_msg}", LogLevel.WARN)
    
    def _on_polling_finished(self):
        """轮询线程结束"""
        poll_log("轮询线程结束")
    
    def process_bilibili_login(self, ticket: str, qr_data: str):
        """处理B站登录（对齐 C++ LoginBH3BiliBili → scanCheck + scanConfirm）"""
        # 崩坏3B站服登录
        uid = self.selected_account.uid
        token = self.selected_account.token
        name = self.selected_account.name

        # 停止扫描（与 C++ 检测到后即 stop() 一致）
        if self.is_screen_scanning:
            self.toggle_screen_scan()
        if self.is_stream_scanning:
            self.toggle_stream_scan()

        # 对齐 C++ scanCheck：先通知服务器已扫描
        if not self.api.bh3_qrcode_scan(ticket):
            gui_log("扫码确认失败！", LogLevel.WARN)
            QMessageBox.warning(self, "提示", "扫码确认失败！")
            return

        result = self.api.bh3_qrcode_confirm(ticket, uid, token, name)

        bili_log(f"B站登录结果: ticket={ticket[:12]}..., uid={uid}, result={result.name}", LogLevel.DEBUG, console_only=True)
        if result == ScanRet.SUCCESS:
            gui_log("扫码登录成功！")
            QMessageBox.information(self, "登录成功", "扫码登录成功！")

            if self.action_auto_exit.isChecked():
                self.close()
        else:
            gui_log(f"扫码登录失败 (错误码: {result})", LogLevel.ERROR)
            QMessageBox.warning(self, "登录失败", f"扫码登录失败 (错误码: {result})")
    
    def start_screen_scan(self):
        """开始屏幕扫描"""
        if self.is_screen_scanning:
            return

        # 如果有选中账号，确保API类型与账号匹配
        if self.selected_account:
            self.api.set_game_type(GameType(self.selected_account.game_type))
            self.api.set_server_type(ServerType(self.selected_account.server_type))
            main_log(f"开始屏幕扫描: 账号={self.selected_account.name}, game={self.selected_account.game_type}, server={self.selected_account.server_type}")
        else:
            main_log("开始屏幕扫描: 未选择账号")

        self.screen_scanner = ScreenScanner()
        # 连接已验证的游戏二维码信号（C++ 风格：已在扫描器中验证 URL[79:82] 匹配游戏类型）
        self.screen_scanner.qrcode_game_detected.connect(self.on_qrcode_detected)
        self.screen_scanner.scan_finished.connect(self.on_scan_finished)
        self.screen_scanner.scan_error.connect(self.on_scan_error)

        self.screen_scanner.start()
        self.is_screen_scanning = True

        self.btn_screen_scan.setText("停止监视")
        self.btn_screen_scan.setStyleSheet("background-color: #4CAF50;")
        self.scan_status_label.setText("正在监视屏幕...")
    
    def stop_screen_scan(self):
        """停止屏幕扫描"""
        if self.screen_scanner:
            self.screen_scanner.stop()
            try:
                self.screen_scanner.quit()
                if not self.screen_scanner.wait(2000):  # 等待最多2秒
                    scanner_log("屏幕扫描线程未能及时停止", LogLevel.WARN)
            except Exception as e:
                main_log(f"停止屏幕扫描时出错: {e}", LogLevel.WARN)
            self.screen_scanner = None

        self.is_screen_scanning = False
        self.btn_screen_scan.setText("监视屏幕")
        self.btn_screen_scan.setStyleSheet("")
        self.scan_status_label.setText("屏幕监视已停止")
        QTimer.singleShot(1000, lambda: self.scan_status_label.clear())
        main_log("屏幕扫描已停止")
    
    def toggle_screen_scan(self):
        """切换屏幕扫描状态"""
        if self.is_screen_scanning:
            self.stop_screen_scan()
        else:
            self.start_screen_scan()
    
    def start_stream_scan(self):
        """开始直播流扫描"""
        room_id = self.room_id_edit.text().strip()

        if not room_id:
            gui_log("请输入直播间ID", LogLevel.WARN)
            QMessageBox.warning(self, "输入错误", "请输入直播间ID")
            return

        # 获取直播流URL
        platform = LivePlatform(self.platform_combo.currentIndex())
        live_info = get_live_info(platform, room_id)

        if live_info.status == LiveStreamStatus.Absent:
            gui_log("直播间不存在", LogLevel.WARN)
            QMessageBox.warning(self, "错误", "直播间不存在")
            return

        if live_info.status == LiveStreamStatus.NotLive:
            gui_log("该直播间未开播", LogLevel.WARN)
            QMessageBox.warning(self, "提示", "该直播间未开播")
            return

        if live_info.status != LiveStreamStatus.Normal or not live_info.link:
            if live_info.status == LiveStreamStatus.Error and platform == LivePlatform.BiliBili:
                gui_log("无法获取B站直播流（请检查控制台日志排查：Cookie是否过期、直播间是否限区等）", LogLevel.ERROR)
                QMessageBox.warning(self, "错误", "无法获取B站直播流\n请检查控制台日志排查：Cookie是否过期、直播间是否限区等")
            else:
                gui_log("无法获取直播流", LogLevel.ERROR)
                QMessageBox.warning(self, "错误", "无法获取直播流")
            return
        
        self.current_stream_url = live_info.link
        main_log(f"开始直播扫描: 平台={platform.name}, 房间号={room_id}, 流URL={live_info.link[:50]}...")
        
        # 启动 StreamScanner 从直播流中检测二维码
        self.stream_scanner = StreamScanner()
        self.stream_scanner.set_stream_url(self.current_stream_url)
        # 对齐 C++ GetStreamLink：B站流必须带 Referer/Origin/User-Agent 头，否则 CDN 拒绝(403)
        if platform == LivePlatform.BiliBili:
            self.stream_scanner.set_headers({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "Chrome/126.0.0.0 Safari/537.36",
                "Referer": "https://live.bilibili.com/",
                "Origin": "https://live.bilibili.com",
            })
        self.stream_scanner.qrcode_game_detected.connect(self.on_qrcode_detected)
        self.stream_scanner.scan_finished.connect(self.on_scan_finished)
        self.stream_scanner.scan_error.connect(self.on_scan_error)
        
        self.stream_scanner.start()
        self.is_stream_scanning = True
        
        self.btn_stream_scan.setText("停止监视")
        self.btn_stream_scan.setStyleSheet("background-color: #4CAF50;")
        self.scan_status_label.setText(f"正在监视直播间 {room_id}...")
    
    def stop_stream_scan(self):
        """停止直播流扫描"""
        if self.stream_scanner:
            self.stream_scanner.stop()
            try:
                self.stream_scanner.quit()
                if not self.stream_scanner.wait(2000):  # 等待最多2秒
                    scanner_log("直播扫描线程未能及时停止", LogLevel.WARN)
            except Exception as e:
                main_log(f"停止直播扫描时出错: {e}", LogLevel.WARN)
            self.stream_scanner = None

        self.is_stream_scanning = False
        self.btn_stream_scan.setText("监视直播间")
        self.btn_stream_scan.setStyleSheet("")
        self.scan_status_label.setText("直播监视已停止")
        QTimer.singleShot(1000, lambda: self.scan_status_label.clear())
        main_log("直播扫描已停止")
    
    def toggle_stream_scan(self):
        """切换直播流扫描状态"""
        if self.is_stream_scanning:
            self.stop_stream_scan()
        else:
            self.start_stream_scan()
    
    # ========== 菜单操作 ==========
    
    def show_add_account_dialog(self):
        """显示添加账号对话框"""
        dialog = LoginWindow(self)
        dialog.login_success.connect(self.on_login_success)
        # 连接二维码信号
        dialog.qr_fetched.connect(dialog._on_qr_fetched)
        dialog.qr_fetch_error.connect(dialog._on_qr_fetch_error)
        dialog.exec()
    
    def on_login_success(self, name: str, token: str, uid: str, mid: str, server_type: str):
        """登录成功处理"""
        # 判断服务器类型
        if "B服" in server_type or "BiliBili" in server_type:
            server = ServerType.BiliBili
            # B站登录的是崩坏3
            game = GameType.Honkai3
        else:
            server = ServerType.Official
            game = GameType.Genshin

        # B站登录时 name 可能就是 uid，需要使用传入的 name
        account_name = name if name and name != uid else f"用户{uid}"

        self.account_manager.add_account(
            uid=uid,
            name=account_name,
            token=token,
            server_type=server,
            game_type=game,
            mid=mid
        )
        self.load_accounts()
        main_log(f"账号已添加: name={account_name}, uid={uid}, server={server.name}, game={game.name}, mid={mid}")
        gui_log(f"账号 {account_name}({uid}) 添加成功")
    
    def delete_selected_account(self):
        """删除选中的账号"""
        current_row = self.account_table.currentRow()
        if current_row < 0:
            gui_log("请先选择要删除的账号", LogLevel.WARN)
            QMessageBox.warning(self, "提示", "请先选择要删除的账号")
            return

        reply = QMessageBox.question(
            self, "确认删除", "确定要删除这个账号吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            uid = self.account_table.item(current_row, 1).text()
            server_type = self.get_server_type_from_table(current_row)

            main_log(f"删除账号: uid={uid}, server_type={server_type}")
            self.account_manager.remove_account(uid, server_type)
            self.load_accounts()
            gui_log(f"账号已删除: uid={uid}")

    def set_default_account(self):
        """设置为默认账号（菜单调用）"""
        current_row = self.account_table.currentRow()
        if current_row < 0:
            gui_log("请先选择一个账号", LogLevel.WARN)
            QMessageBox.warning(self, "提示", "请先选择一个账号")
            return
        
        self.select_account_by_row(current_row)
        self.set_as_default()

    def eventFilter(self, obj, event):
        """事件过滤器：处理表格悬浮高亮"""
        if obj == self.account_table.viewport():
            from PyQt6.QtCore import QEvent
            if event.type() == QEvent.Type.MouseMove:
                index = self.account_table.indexAt(event.pos())
                if index.isValid():
                    self._update_hover_row(index.row())
                else:
                    self._update_hover_row(-1)
                return True
            elif event.type() == QEvent.Type.Leave:
                self._update_hover_row(-1)  # 鼠标离开时清除高亮
                return True
        return super().eventFilter(obj, event)
    
    def open_config_file(self):
        """打开配置文件（内置编辑器）"""
        config_dir = get_base_dir() / "Config"
        if not config_dir.exists():
            gui_log("Config 目录不存在", LogLevel.WARN)
            QMessageBox.information(self, "提示", "Config 目录不存在")
            return

        json_files = list(config_dir.glob("*.json"))
        if not json_files:
            gui_log("Config 目录下没有配置文件", LogLevel.WARN)
            QMessageBox.information(self, "提示", "Config 目录下没有配置文件")
            return

        editor = ConfigEditor(get_base_dir(), self)
        editor.file_saved.connect(self._on_config_file_saved)
        editor.exec()

    def _on_config_file_saved(self, filepath: str):
        """配置文件编辑器保存后，重新加载并刷新界面"""
        if "userinfo" in filepath:
            self.config.reload()
            self.load_accounts()
            gui_log(f"配置文件已更新，账号列表已刷新")
        elif "config" in filepath:
            self.config.reload()

    def show_cookie_refresh_dialog(self):
        """打开 Cookie 刷新弹窗"""
        dialog = CookieRefreshDialog(self)
        dialog.exec()

    def check_for_updates(self):
        """检查更新"""
        from utils.update import check_for_updates
        info = check_for_updates()
        if info.get("check_failed"):
            gui_log("检查更新失败，请检查网络连接")
            QMessageBox.warning(self, "检查更新", "检查更新失败，无法连接到 GitHub。\n请检查网络连接后重试。")
        elif info.get("no_release"):
            gui_log("仓库暂无 Release 版本")
            QMessageBox.information(self, "检查更新", "GitHub 仓库尚未发布任何 Release 版本，暂无更新源。")
        elif info["has_update"]:
            gui_log(f"发现新版本: {info['latest_version']}")
            reply = QMessageBox.information(
                self, "发现新版本",
                f"当前版本: {info['current_version']}\n"
                f"最新版本: {info['latest_version']}\n\n"
                f"是否立即下载更新？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                # 后台线程下载，避免 UI 卡死
                self._start_download_update(info["download_url"])
        else:
            gui_log("当前已是最新版本")
            QMessageBox.information(self, "检查更新", "当前已是最新版本")

    def _start_download_update(self, download_url: str):
        """启动后台下载更新（带进度对话框）"""
        self._download_progress = QProgressDialog("正在下载更新...", None, 0, 100, self)
        self._download_progress.setWindowTitle("下载更新")
        self._download_progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._download_progress.setMinimumDuration(0)
        self._download_progress.setAutoClose(False)
        self._download_progress.setAutoReset(False)
        self._download_progress.setCancelButton(None)  # 下载期间不允许取消

        # 创建工作线程
        self._download_thread = QThread()
        self._download_worker = DownloadUpdateWorker(download_url)
        self._download_worker.moveToThread(self._download_thread)

        # 连接信号
        self._download_worker.progress.connect(self._download_progress.setValue)
        self._download_worker.finished.connect(self._on_download_finished)
        self._download_thread.started.connect(self._download_worker.run)
        # worker 完成后清理
        self._download_worker.finished.connect(self._download_thread.quit)
        self._download_worker.finished.connect(self._download_worker.deleteLater)
        self._download_thread.finished.connect(self._download_thread.deleteLater)

        self._download_thread.start()

    def _on_download_finished(self, success: bool, error_msg: str):
        """下载完成回调"""
        if hasattr(self, "_download_progress"):
            self._download_progress.close()

        if success:
            gui_log("更新已下载，程序即将退出并重启...")
            QMessageBox.information(self, "下载完成", "更新已下载，程序即将退出并启动安装程序。")
            QApplication.instance().quit()
        else:
            gui_log(f"下载更新失败: {error_msg}")
            QMessageBox.warning(self, "下载失败", f"下载更新失败。\n{error_msg}\n请检查网络连接后重试。")
    
    def show_about(self):
        """显示关于"""
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        app_name = app.applicationName()
        app_ver = app.applicationVersion()
        QMessageBox.about(
            self, "关于",
            f"<h3>{app_name}</h3>"
            f"<p>版本: {app_ver}</p>"
            f"<p>基于PyQt6+OpenCV实现</p>"
            f"<p>支持: 原神、星穹铁道、绝区零、崩坏3</p>"
            f"<p>本项目为免费开源项目，用于学习和研究，禁止商业化用途。</p>"
            F"<p>本项目参考项目: <a href='{self.GITHUB_URL}'>{self.GITHUB_URL}</a>制作</p>"
        )

    def show_changelog(self):
        """显示更新日志"""
        dialog = QDialog(self)
        dialog.setWindowTitle("更新日志")
        dialog.setFixedSize(500, 400)
        dialog.setSizeGripEnabled(False)

        layout = QVBoxLayout(dialog)

        app = QApplication.instance()
        ver_label = QLabel(f"<b>{app.applicationName()} v{app.applicationVersion()}</b>")
        layout.addWidget(ver_label)

        changelog = QTextEdit()
        changelog.setReadOnly(True)
        changelog.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        changelog.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        changelog.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        changelog.setHtml(self.CHANGELOG_HTML)
        layout.addWidget(changelog)

        dialog.exec()
    
    def open_github_issues(self):
        """打开GitHub Issues"""
        import webbrowser
        webbrowser.open(self.GITHUB_ISSUES_URL)
    
    def open_homepage(self):
        """打开项目主页"""
        import webbrowser
        webbrowser.open(self.GITHUB_URL)
    
    def closeEvent(self, event):
        """窗口关闭事件"""
        main_log("正在关闭，清理所有资源...")

        # 停止登录轮询线程
        main_log("停止登录轮询...")
        self._stop_polling()

        # 停止所有扫描
        if self.is_screen_scanning:
            main_log("停止屏幕扫描...")
            self.stop_screen_scan()

        if self.is_stream_scanning:
            main_log("停止直播扫描...")
            self.stop_stream_scan()

        # 确保扫描线程完全停止
        if self.screen_scanner:
            self.screen_scanner.quit()
            self.screen_scanner.wait(1000)
            self.screen_scanner = None

        if self.stream_scanner:
            self.stream_scanner.quit()
            self.stream_scanner.wait(1000)
            self.stream_scanner = None

        main_log("所有资源已清理")
        event.accept()

    def restart_app(self):
        """重启程序（热更新）"""
        main_log("准备热更新...")
        restart_program()
        self.should_restart = True
        self.close()
