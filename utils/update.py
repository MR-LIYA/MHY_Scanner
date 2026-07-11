"""
热更新模块
处理程序的热更新、重启等功能
修复GitCode API 404问题，双API兜底：v5标准 + v4兼容
"""
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

VERSION = "1.0.4"
class UpdateManager:
    """热更新管理器"""
    # GitHub Release API
    GITHUB_API_URL = "https://api.github.com/repos/MR-LIYA/MHY_Scanner/releases?per_page=1"
    # GitCode Release API
    GITCODE_V5_API = "https://api.gitcode.com/api/v5/repos/MR-LIYA/MHY_Scanner/releases?per_page=1"
    
    # 安装包直链
    GITHUB_INSTALLER_URL = "https://github.com/MR-LIYA/MHY_Scanner/releases/download/main/MHY_Scanner_Setup.exe"
    GITCODE_INSTALLER_URL = "https://gitcode.com/MR-LIYA/MHY_Scanner/releases/download/main/MHY_Scanner_Setup.exe"

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
                    return self._current_version
            except Exception:
                pass
            try:
                from __init__ import __version__
                self._current_version = __version__
            except Exception:
                self._current_version = VERSION
        return self._current_version

    def restart_program(self):
        """重启程序，加载最新代码"""
        update_log("正在重启程序...")
        self._cleanup_temp_files()
        python = sys.executable
        try:
            subprocess.Popen(
                [python, self.main_script],
                cwd=os.path.dirname(self.main_script),
                creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0
            )
            update_log("新进程已启动")
            return True
        except Exception as e:
            error(f"重启失败: {e}\n{traceback.format_exc()}")
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
                    update_log(f"已清理目录: {temp}")
                elif os.path.isfile(path):
                    os.remove(path)
                    update_log(f"已清理文件: {temp}")
            except Exception:
                pass

    def _fetch_release_info(self, api_url: str) -> tuple[dict, bool]:
        """通用API请求封装，统一处理JSON解析、状态码异常"""
        try:
            headers = {"Accept": "application/json"}
            response = requests.get(api_url, timeout=20, verify=False, headers=headers)
            if response.status_code == 404:
                update_log(f"API 404不存在：{api_url}", LogLevel.WARN)
                return {}, False
            if response.status_code != 200:
                update_log(f"API请求异常 状态码{response.status_code} URL:{api_url}", LogLevel.WARN)
                return {}, False
            # 解析JSON，过滤XML报错页面
            try:
                releases = response.json()
            except Exception:
                update_log(f"API返回非JSON数据（XML错误页）{api_url}", LogLevel.WARN)
                return {}, False
            if not isinstance(releases, list) or len(releases) == 0:
                update_log(f"该源无Release发布记录 {api_url}", LogLevel.INFO)
                return {}, False
            return releases[0], True
        except Exception as err:
            update_log(f"请求API网络异常 {api_url}：{str(err)}", LogLevel.WARN)
            return {}, False

    def check_for_updates(self) -> dict:
        """更新检查逻辑：GitCode v5 → GitCode v4 → GitHub 三级降级"""
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
        # 三级源依次尝试
        source_list = [
            ("gitcode_v5", self.GITCODE_V5_API),
            ("github", self.GITHUB_API_URL)
        ]
        used_source_name = ""
        for source_name, api_url in source_list:
            data, success = self._fetch_release_info(api_url)
            if success:
                release_data = data
                used_source_name = source_name
                break
        else:
            # 全部源请求失败
            result["check_failed"] = True
            return result
        result["used_source"] = used_source_name

        # 解析版本标签（兼容两种API返回字段）
        tag_text = release_data.get("name", "") or release_data.get("tag_name", "")
        if not tag_text:
            update_log("Release无法读取版本标签", LogLevel.WARN)
            result["check_failed"] = True
            return result
        latest_ver = tag_text.lstrip("Vv")
        desc = release_data.get("body", "").strip() or tag_text

        # 读取资产下载链接，无exe则使用硬编码地址
        assets = release_data.get("assets", [])
        if assets:
            result["download_url"] = assets[0].get("browser_download_url", "")
            if not any(item.get("name", "").endswith(".exe") for item in assets):
                result["download_url"] = self.GITCODE_INSTALLER_URL if "gitcode" in used_source_name else self.GITHUB_INSTALLER_URL
        else:
            result["download_url"] = self.GITCODE_INSTALLER_URL if "gitcode" in used_source_name else self.GITHUB_INSTALLER_URL

        # 校验版本格式
        if not self._is_valid_version(latest_ver):
            update_log(f"非法版本号：{tag_text}", LogLevel.WARN)
            result["check_failed"] = True
            return result
        result["latest_version"] = latest_ver
        result["description"] = desc
        update_log(f"本地版本{self.current_version}，远程最新{latest_ver}，查询源：{used_source_name}")
        if self._compare_version(latest_ver, self.current_version) > 0:
            result["has_update"] = True
            update_log(f"检测到新版本 {latest_ver}")
        return result

    def download_and_apply_update(self, download_url: str = "", progress_callback=None) -> bool:
        """下载安装包，GitCode失败自动切换GitHub"""
        if not download_url:
            check_res = self.check_for_updates()
            download_url = self.GITCODE_INSTALLER_URL if "gitcode" in check_res["used_source"] else self.GITHUB_INSTALLER_URL
        update_log(f"开始下载安装包：{download_url}")
        try:
            resp = requests.get(download_url, stream=True, timeout=600, verify=False)
            if resp.status_code != 200:
                error(f"下载HTTP {resp.status_code}")
                if "gitcode.com" in download_url:
                    update_log("GitCode国内镜像下载失败，切换GitHub重试")
                    return self.download_and_apply_update(self.GITHUB_INSTALLER_URL, progress_callback)
                return False
            temp_exe = os.path.join(tempfile.gettempdir(), "MHY_Scanner_Setup.exe")
            total_size = int(resp.headers.get("content-length", 0))
            downloaded_size = 0
            with open(temp_exe, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if progress_callback and total_size > 0:
                            progress_callback(int(downloaded_size / total_size * 100))
            update_log(f"安装包下载完成：{temp_exe} 总字节{downloaded_size}")
            update_log("启动安装程序，当前程序即将退出")
            subprocess.Popen(
                [temp_exe],
                creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0
            )
            return True
        except Exception as e:
            error(f"下载异常：{e}\n{traceback.format_exc()}")
            if "gitcode.com" in download_url:
                update_log("GitCode镜像下载异常，切换GitHub重试")
                return self.download_and_apply_update(self.GITHUB_INSTALLER_URL, progress_callback)
            return False

    def _compare_version(self, v1: str, v2: str) -> int:
        """版本对比 v1>v2返回1，v1<v2返回-1，相等0"""
        def split_ver(s):
            return [int(x) for x in s.split(".")]
        v1_list = split_ver(v1)
        v2_list = split_ver(v2)
        max_len = max(len(v1_list), len(v2_list))
        for i in range(max_len):
            p1 = v1_list[i] if i < len(v1_list) else 0
            p2 = v2_list[i] if i < len(v2_list) else 0
            if p1 > p2:
                return 1
            elif p1 < p2:
                return -1
        return 0

    def _is_valid_version(self, version: str) -> bool:
        """校验 x.x.x 数字版本格式"""
        try:
            parts = version.split(".")
            return len(parts) >= 2 and all(p.isdigit() for p in parts)
        except:
            return False

    def reload_module(self, module_name: str):
        """卸载并重新加载模块，用于热重载代码"""
        if module_name in sys.modules:
            del sys.modules[module_name]
            update_log(f"已卸载缓存模块：{module_name}")
        try:
            import importlib
            mod = importlib.import_module(module_name)
            update_log(f"模块重载完成：{module_name}")
            return mod
        except Exception as e:
            error(f"重载模块失败 {module_name}: {e}\n{traceback.format_exc()}")
            return None

# 全局单例
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
