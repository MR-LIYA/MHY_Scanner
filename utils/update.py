import sys
import os
import subprocess
import shutil
import traceback
import tempfile
import requests
from core.logger import update_log, error, LogLevel
# Windows 下可能缺少 SSL 证书，禁用证书验证
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import ctypes
from ctypes import wintypes

VERSION = "1.0.6"
class UpdateManager:
    """热更新管理器"""
    # 仅使用GitHub latest单点接口，不使用列表接口
    GITHUB_LATEST_API = "https://api.github.com/repos/MR-LIYA/MHY_Scanner/releases/latest"
    # GitCode v5 国内源备用
    GITCODE_V5_API = "https://api.gitcode.com/api/v5/repos/MR-LIYA/MHY_Scanner/releases?per_page=1"

    def __init__(self):
        self.script_path = os.path.abspath(__file__)
        self.main_script = os.path.join(os.path.dirname(os.path.dirname(self.script_path)), "main.py")
        self._current_version = None

    @property
    def current_version(self) -> str:
        """获取当前版本号（优先读取 QApplication.applicationVersion()，与 main.py 统一）"""
        if self._current_version is None:
            try:
                from PyQt6.QtWidgets import QApplication
                app = QApplication.instance()
                if app and app.applicationVersion():
                    self._current_version = app.applicationVersion()
                    update_log(f"[版本读取] 从QApplication获取本地版本: {self._current_version}", LogLevel.INFO)
                    return self._current_version
            except Exception as e:
                update_log(f"[版本读取] QApplication读取失败: {str(e)}", LogLevel.WARN)
            try:
                from __init__ import __version__
                self._current_version = __version__
                update_log(f"[版本读取] 从__init__获取本地版本: {self._current_version}", LogLevel.INFO)
            except Exception as e:
                update_log(f"[版本读取] __init__读取失败，使用内置常量VERSION={VERSION}", LogLevel.WARN)
                self._current_version = VERSION
        return self._current_version

    def restart_program(self):
        """重启程序，加载最新代码"""
        update_log(f"[重启] 正在清理缓存并重启程序...", LogLevel.INFO)
        self._cleanup_temp_files()
        python = sys.executable
        try:
            subprocess.Popen(
                [python, self.main_script],
                cwd=os.path.dirname(self.main_script),
                creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0
            )
            update_log(f"[重启] 新程序进程已启动", LogLevel.INFO)
            return True
        except Exception as e:
            error(f"[重启失败] {e}\n{traceback.format_exc()}")
            return False

    def _cleanup_temp_files(self):
        """清理编译缓存、临时文件"""
        temp_dir = os.path.dirname(self.main_script)
        temp_files = ["__pycache__", ".pytest_cache", "tempCodeRunnerFile.py"]
        for temp in temp_files:
            path = os.path.join(temp_dir, temp)
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                    update_log(f"[缓存清理] 已删除目录: {temp}", LogLevel.INFO)
                elif os.path.isfile(path):
                    os.remove(path)
                    update_log(f"[缓存清理] 已删除文件: {temp}", LogLevel.INFO)
            except Exception as e:
                update_log(f"[缓存清理] 删除 {temp} 失败: {str(e)}", LogLevel.WARN)

    def _fetch_release_info(self, api_url: str) -> tuple[dict, bool]:
        """通用API请求，兼容GitCode数组 / GitHub latest单字典返回格式"""
        update_log(f"[接口请求] 开始拉取版本接口: {api_url}", LogLevel.INFO)
        try:
            headers = {"Accept": "application/json"}
            response = requests.get(api_url, timeout=20, verify=False, headers=headers)
            if response.status_code == 404:
                update_log(f"[接口请求] API 404 不存在：{api_url}", LogLevel.WARN)
                return {}, False
            if response.status_code != 200:
                update_log(f"[接口请求] HTTP异常 状态码{response.status_code} URL:{api_url}", LogLevel.WARN)
                return {}, False
            # 解析JSON
            try:
                data = response.json()
                update_log(f"[接口请求] JSON数据解析成功", LogLevel.INFO)
            except Exception as e:
                update_log(f"[接口请求] 返回非标准JSON {api_url}, 错误:{str(e)}", LogLevel.WARN)
                return {}, False

            # 区分返回数据类型
            if isinstance(data, list):
                # GitCode分页接口返回数组
                if len(data) == 0:
                    update_log(f"[接口请求] 该源无任何Release记录 {api_url}", LogLevel.INFO)
                    return {}, False
                return data[0], True
            elif isinstance(data, dict):
                # GitHub latest 单对象接口
                return data, True
            else:
                update_log(f"[接口请求] 返回数据格式非法 {api_url}", LogLevel.WARN)
                return {}, False
        except Exception as err:
            update_log(f"[接口请求] 网络异常 {api_url}：{str(err)}", LogLevel.WARN)
            return {}, False

    def check_for_updates(self) -> dict:
        """更新检查：GitCode国内源优先，失败再走GitHub latest接口"""
        result = {
            "has_update": False,
            "no_release": False,
            "check_failed": False,
            "current_version": self.current_version,
            "latest_version": "",
            "description": "",
            "download_url": "",
            "used_source": ""
        }
        release_data = {}
        # 源优先级：国内GitCode > GitHub官方
        source_list = [
            ("gitcode_v5", self.GITCODE_V5_API),
            ("github_latest", self.GITHUB_LATEST_API)
        ]
        used_source_name = ""
        for source_name, api_url in source_list:
            update_log(f"[版本检测] 尝试数据源: {source_name}", LogLevel.INFO)
            data, success = self._fetch_release_info(api_url)
            if success:
                release_data = data
                used_source_name = source_name
                update_log(f"[版本检测] {source_name} 数据源拉取成功，终止切换下一个源", LogLevel.INFO)
                break
            update_log(f"[版本检测] {source_name} 请求失败，切换下一数据源", LogLevel.WARN)
        else:
            # 两个接口全部请求失败
            result["check_failed"] = True
            update_log("[版本检测] GitCode、GitHub两个数据源全部请求失败，更新检查终止", LogLevel.ERROR)
            return result
        result["used_source"] = used_source_name

        # 提取版本标签
        tag_text = release_data.get("name", "") or release_data.get("tag_name", "")
        if not tag_text:
            update_log("[版本解析] Release数据中无法读取版本标签", LogLevel.WARN)
            result["check_failed"] = True
            return result
        latest_ver = tag_text.lstrip("Vv")
        desc = release_data.get("body", "").strip() or tag_text
        update_log(f"[版本解析] 原始tag={tag_text}，清洗后远程版本={latest_ver}", LogLevel.INFO)

        # 遍历资源，仅提取exe安装包链接
        assets = release_data.get("assets", [])
        exe_url = ""
        update_log(f"[资源筛选] 当前Release包含资源总数：{len(assets)}，仅筛选.exe安装包", LogLevel.INFO)
        for asset in assets:
            file_name = asset.get("name", "").lower()
            dl_link = asset.get("browser_download_url", "")
            # 跳过图片资源
            if file_name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                continue
            # 匹配安装包exe
            if file_name.endswith(".exe"):
                exe_url = dl_link
                update_log(f"[资源筛选] 匹配到安装包，下载链接：{exe_url}", LogLevel.INFO)
                break
        result["download_url"] = exe_url
        if not exe_url:
            update_log("[资源筛选] 当前Release未上传exe安装包，无自动更新下载链接", LogLevel.WARN)

        # 校验版本数字格式
        if not self._is_valid_version(latest_ver):
            update_log(f"[版本校验] 远程版本号格式非法，原始tag={tag_text}，清洗后={latest_ver}", LogLevel.WARN)
            result["check_failed"] = True
            return result
        result["latest_version"] = latest_ver
        result["description"] = desc

        # 版本对比逻辑
        local_ver = self.current_version
        cmp_ret = self._compare_version(latest_ver, local_ver)
        update_log(f"[版本对比] 本地版本 {local_ver} | 远程最新 {latest_ver} | 对比结果码：{cmp_ret}（1=有新版，0=一致，-1=本地更新）", LogLevel.INFO)
        if cmp_ret > 0:
            result["has_update"] = True
            update_log(f"[版本对比] 检测到新版本 {latest_ver}，将弹出更新窗口", LogLevel.INFO)
        else:
            update_log("[版本对比] 本地版本 ≥ 远程版本，判定为已是最新版本", LogLevel.INFO)
        return result

    def download_and_apply_update(self, download_url: str = "", progress_callback=None) -> bool:
        """下载更新安装包并启动安装程序"""
        if not download_url:
            error("[更新下载] 未获取有效安装包下载链接，无法更新")
            return False
        update_log(f"[更新下载] 开始下载安装包：{download_url}", LogLevel.INFO)
        try:
            resp = requests.get(download_url, stream=True, timeout=600, verify=False)
            if resp.status_code != 200:
                error(f"[更新下载] 链接访问失败，HTTP {resp.status_code}")
                return False
            
            temp_dir = tempfile.gettempdir()
            temp_exe_short = os.path.join(temp_dir, "MHY_Scanner_Setup.exe")
            total_size = int(resp.headers.get("content-length", 0))
            downloaded_size = 0

            with open(temp_exe_short, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if progress_callback and total_size > 0:
                            progress_callback(int(downloaded_size / total_size * 100))
            
            # 文件完整性校验
            if total_size > 0 and downloaded_size != total_size:
                error(f"[更新下载] 安装包下载残缺，预期{total_size}字节，实际{downloaded_size}字节")
                if os.path.exists(temp_exe_short):
                    os.remove(temp_exe_short)
                return False
            
            # 短路径转长路径，解决Windows8.3短路径报错
            def get_long_path(path: str) -> str:
                buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
                ctypes.windll.kernel32.GetLongPathNameW(path, buf, wintypes.MAX_PATH)
                return buf.value
            temp_exe_long = get_long_path(temp_exe_short)

            update_log(f"[更新下载] 安装包下载完成，临时路径：{temp_exe_long}，总大小{downloaded_size}字节", LogLevel.INFO)
            update_log(f"[更新安装] 即将启动安装程序，当前主程序会退出", LogLevel.INFO)
            subprocess.Popen(
                [temp_exe_long],
                creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0
            )
            return True
        except Exception as e:
            error(f"[更新下载] 下载流程异常：{e}\n{traceback.format_exc()}")
            return False

    def _compare_version(self, v1: str, v2: str) -> int:
        """版本号对比函数
        v1=远程版本，v2=本地版本
        返回 1: v1更新，-1:v2更新，0:完全一致
        """
        def split_ver(s):
            return [int(x) for x in s.split(".")]
        v1_list = split_ver(v1)
        v2_list = split_ver(v2)
        max_len = max(len(v1_list), len(v2_list))
        update_log(f"[版本拆分] 远程{v1_list} 本地{v2_list}", LogLevel.DEBUG)
        for i in range(max_len):
            p1 = v1_list[i] if i < len(v1_list) else 0
            p2 = v2_list[i] if i < len(v2_list) else 0
            if p1 > p2:
                update_log(f"[版本分段] 第{i}段 {p1} > {p2}，远程版本更新", LogLevel.DEBUG)
                return 1
            elif p1 < p2:
                update_log(f"[版本分段] 第{i}段 {p1} < {p2}，本地版本更新", LogLevel.DEBUG)
                return -1
        update_log("[版本拆分] 两段版本号完全相等", LogLevel.DEBUG)
        return 0

    def _is_valid_version(self, version: str) -> bool:
        """校验版本格式为 x.x.x 纯数字分段"""
        try:
            parts = version.split(".")
            valid = len(parts) >= 2 and all(p.isdigit() for p in parts)
            update_log(f"[格式校验] 版本 {version} 校验结果：{valid}", LogLevel.DEBUG)
            return valid
        except Exception as e:
            update_log(f"[格式校验] 版本 {version} 解析异常：{str(e)}", LogLevel.WARN)
            return False

    def reload_module(self, module_name: str):
        """卸载并重新加载模块，用于热重载代码"""
        if module_name in sys.modules:
            del sys.modules[module_name]
            update_log(f"[模块重载] 已卸载缓存模块：{module_name}", LogLevel.INFO)
        try:
            import importlib
            mod = importlib.import_module(module_name)
            update_log(f"[模块重载] 模块 {module_name} 重载完成", LogLevel.INFO)
            return mod
        except Exception as e:
            error(f"[模块重载] 重载 {module_name} 失败: {e}\n{traceback.format_exc()}")
            return None

# 全局单例导出
_update_manager = None
def get_update_manager() -> UpdateManager:
    global _update_manager
    if _update_manager is None:
        _update_manager = UpdateManager()
    return _update_manager

def restart_program() -> bool:
    return get_update_manager().restart_program()

def check_for_updates() -> dict:
    return get_update_manager().check_for_updates()

def reload_module(module_name: str):
    return get_update_manager().reload_module(module_name)

def download_and_apply_update(download_url: str = "", progress_callback=None) -> bool:
    return get_update_manager().download_and_apply_update(download_url, progress_callback)
