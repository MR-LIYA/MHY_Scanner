import ctypes
from ctypes import wintypes
import atexit
from core.logger import Logger, LogLevel

# 全局唯一互斥标识
MUTEX_GLOBAL_NAME = r"Global\MHY_Scanner_SingleInstance_Mutex_20260720"
_hMutex = None


def check_single_instance() -> bool:
    global _hMutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    CreateMutexW = kernel32.CreateMutexW
    GetLastError = kernel32.GetLastError
    CloseHandle = kernel32.CloseHandle
    ERROR_ALREADY_EXISTS = 183

    # 布尔参数直接填 1（TRUE）
    _hMutex = CreateMutexW(None, 1, MUTEX_GLOBAL_NAME)
    if not _hMutex:
        err_code = GetLastError()
        Logger.single_log(f"创建单实例互斥体失败，系统错误码：{err_code}", LogLevel.ERROR)
        return False

    if GetLastError() == ERROR_ALREADY_EXISTS:
        CloseHandle(_hMutex)
        _hMutex = None
        Logger.single_log("检测到MHY_Scanner已运行，拦截重复启动", LogLevel.WARN)
        return False

    def release_mutex_handle():
        global _hMutex
        if _hMutex is not None:
            CloseHandle(_hMutex)
            Logger.single_log("程序正常退出，释放全局互斥对象", LogLevel.INFO)
            _hMutex = None

    atexit.register(release_mutex_handle)
    Logger.single_log("单实例校验通过，当前为唯一运行实例", LogLevel.INFO)
    return True
