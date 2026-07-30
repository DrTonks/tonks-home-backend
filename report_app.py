
import time
import requests
import win32gui
import win32process
import psutil
import ctypes
import signal
import atexit
import sys
import os

user32 = ctypes.windll.User32

APP_MAP = {
    "StartMenuExperienceHost.exe": "开始菜单",
    "electron.exe": "Electron应用开发",
    "ShellExperienceHost.exe": "操作菜单",
    "OpenWith.exe": "打开方式",
    "Notepad.exe": "记事本",
    "SearchUI.exe": "搜索",
    "SearchHost.exe": "开始菜单",
    "RuntimeBroker.exe": "运行时代理",
    "filezilla.exe": "FileZilla",
    "Unknown": "睡眠状态",
    "LockAPP.exe": "锁屏",
    "LockApp.exe": "锁屏",
    "LockAPP": "锁屏",
    "PhoneExperienceHost.exe": "电脑-手机助手",
    "Photos.exe": "照片查看器",
    "WindowsTerminal.exe": "Terminal",
    "cmd.exe": "命令提示符",
    "ApplicationFrameHost.exe": "系统设置",
    "WINWORD.EXE": "Word 文档",
    "EXCEL.EXE": "Excel表格",
    "POWERPNT.EXE": "PPT演示文稿",
    "ONENOTE.EXE": "OneNote",
    "Typora.exe": "Typora编辑器",
    "msedge.exe": "Edge浏览器",
    "Code.exe": "VSCode 编辑器",
    "Clash for Windows.exe": "Clash",
    "Taskmgr.exe": "任务管理器",
    "steamwebhelper.exe": "Steam",
    "WeChat.exe": "微信",
    "QQ.exe": "QQ",
    "explorer.exe": "文件资源管理器",
    "QQMusic.exe": "QQ音乐",
    "XMind.exe": "XMind思维导图",
    "WPS.exe": "WPS办公",
    "WPSPDF.exe": "WPS PDF",
    "WPSWriter.exe": "WPS文字",
    "WPSPresentation.exe": "WPS演示",
    "WPSSpreadsheets.exe": "WPS表格",
    "BaiduNetdisk.exe": "百度网盘",
    "OneDrive.exe": "OneDrive",
    "Notion.exe": "Notion",
    "Postman.exe": "Postman",
    "GitHubDesktop.exe": "GitHub Desktop",
    "obs64.exe": "OBS Studio",
    "Obsidian.exe": "Obsidian",
    "ChatGPT.exe": "ChatGPT",
}

SERVER_URL = os.environ.get('SLEEPY_SERVER_URL', 'http://127.0.0.1:9010').rstrip('/') + '/set'
SECRET = os.environ.get('SLEEPY_STATUS_SECRET', '')

if not SECRET:
    raise RuntimeError('SLEEPY_STATUS_SECRET is required; configure local.env.bat before starting this helper.')


def is_locked():
    return user32.GetForegroundWindow() == 0


def get_active_window_process_name_by_hwnd(hwnd):
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    try:
        process = psutil.Process(pid)
        return process.name()
    except Exception:
        return "Unknown"


def send_report(status, app_name):
    timestamp = int(time.time())
    params = {
        "secret": SECRET,
        "status": status,
        "app_name": app_name,
        "timestamp": timestamp
    }
    try:
        requests.get(SERVER_URL, params=params)
        print(f"已上报：{app_name}")
    except Exception as e:
        print("上报失败：", e)


def report_shutting_down():
    """程序退出时上报关机状态"""
    send_report(1, "关机中")


# 只注册 atexit（SIGINT/SIGTERM 触发 sys.exit 时会自动调用 atexit）
atexit.register(report_shutting_down)
# signal handler 触发 sys.exit 即可，不单独上报
def handle_exit(signum=None, frame=None):
    sys.exit(0)

signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)

last_report = None
while True:
    locked = is_locked()
    if locked:
        status = 1
        show_name = "锁屏"
    else:
        hwnd = win32gui.GetForegroundWindow()
        app_name = get_active_window_process_name_by_hwnd(hwnd)
        status = 0
        show_name = APP_MAP.get(app_name, "未登记应用")

    report_info = (status, show_name)
    if report_info != last_report:
        send_report(status, show_name)
        last_report = report_info

    time.sleep(5)
