import sys
import os
import subprocess
import tempfile
import traceback

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from ui.main_window import MainWindow
from core.logger import main_log, error


def main():
    """主函数"""
    # 必须在 QApplication 实例化之前设置，否则后续动态导入 QtWebEngineWidgets 会失败
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

    # 启用高DPI支持
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)

    # 加载 Qt 中文翻译（汉化系统对话框：字体选择、文件对话框等）
    from PyQt6.QtCore import QTranslator, QLocale, QLibraryInfo
    translator = QTranslator()
    trans_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if translator.load(QLocale.system(), "qtbase", "_", trans_path):
        app.installTranslator(translator)
    translator2 = QTranslator()
    if translator2.load(QLocale.system(), "qt", "_", trans_path):
        app.installTranslator(translator2)

    # 设置应用信息
    app.setApplicationName("MHY_Scanner")
    app.setApplicationVersion("1.0.4")
    app.setOrganizationName("MHY")

    # 设置全局字体
    font = QFont("Microsoft YaHei", 10)
    app.setFont(font)

    # 清理上次更新残留的安装包（无感，不弹提示）
    _cleanup_temp_installer()

    # 创建并显示主窗口
    window = MainWindow()
    window.show()

    # 运行应用
    exit_code = app.exec()

    # 检查是否需要重启
    if window.should_restart:
        main_log("正在重启程序...")
        restart_program()
    sys.exit(exit_code)

def _cleanup_temp_installer():
    """清理临时目录中的安装包（上次更新残留，静默删除）"""
    installer_name = "MHY_Scanner_Setup.exe"
    temp_path = os.path.join(tempfile.gettempdir(), installer_name)
    try:
        if os.path.exists(temp_path):
            os.remove(temp_path)
            main_log(f"已清理临时安装包: {temp_path}")
    except Exception:
        pass  # 文件被占用或权限不足则跳过


def _find_installer() -> str:
    """查找安装包：优先临时目录 → 当前程序目录"""
    installer_name = "MHY_Scanner_Setup.exe"
    # 优先查临时目录（更新下载位置）
    temp_path = os.path.join(tempfile.gettempdir(), installer_name)
    if os.path.exists(temp_path):
        return temp_path
    # 回退到当前程序目录
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    local_path = os.path.join(base_dir, installer_name)
    if os.path.exists(local_path):
        return local_path
    return ""


def restart_program():
    """重启程序：优先运行安装包，否则用 Python 解释器重启"""
    installer = _find_installer()
    if installer:
        try:
            subprocess.Popen([installer], shell=True)
            main_log(f"已启动安装程序: {installer}，当前程序即将退出")
            return
        except Exception as e:
            error(f"启动安装程序失败: {e}\n{traceback.format_exc()}")

    # 开发模式：Python 解释器重启
    python = sys.executable
    script = os.path.abspath(__file__)
    try:
        subprocess.Popen([python, script])
        main_log("已通过 Python 解释器重启")
    except Exception as e:
        error(f"重启失败: {e}\n{traceback.format_exc()}")

if __name__ == "__main__":
    main()
