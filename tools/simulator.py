"""MuMu 模拟器控制工具（长驻会话模式）。

移植自舟项目 <data_drive>:/<projects_root>\\舟\\combine\\core\\simulator.py 并改为**工具模式**：
原版是「构造即连接 + 析构杀 adb」的一次性链路，连续操作场景下每次 click 都
新建 socket、每步都跑 ensure_connected，退出还杀掉共享的 adb server。工具
模式下生命周期如下（连续操作时各资源的保持状态）：

- **adb server**：系统级共享资源（MaaEnd 等也在用），本类**绝不 kill-server**，
  只在 device offline 时通过 reset_server 恢复。
- **adb 连接**：`start()` 建立并探测 screen_size / orientation / abi（各探测
  一次缓存）；断线时任意操作会自动重连。
- **minitouch 进程**：`start()` 后常驻模拟器内（push → chmod → forward
  tcp:1111 → shell 常驻）；`close()` 才停止。/data/local/tmp/minitouch
  **不删除**，下次启动免 push。
- **socket 长连接**：首次点击/滑动时建立，之后全部 click/swipe 复用同一
  连接（TCP_NODELAY），断线自动重建，不再每操作 connect 一次。
- **click/swipe 结束后的 ensure_connected** 已移除：操作失败时才走重连路径，
  连续操作零额外开销。

协议与算法（minitouch d/m/u/c 命令、_convert_coordinates 坐标旋转、三次样
条插值滑动、老式 screencap 截图）与原版一致；仅生命周期与资源管理按上述
调整。

使用::

    from tools.simulator import Simulator

    sim = Simulator(device_addr="127.0.0.1:16384")   # 构造不连接
    sim.click(270, 90)          # 惰性拉起（首用时自动 start），长连接注入
    for p in plans:             # 连续操作：连接/minitouch/socket 全程保持
        sim.click(p.x, p.y)
        sim.swipe(640, 600, 640, 200)
        img = sim.screenshot()
    sim.close()                 # 只停 minitouch + 移 forward，不动 adb

    # 或用 with：退出自动 close
    with Simulator(device_addr="127.0.0.1:16384") as sim:
        sim.click(100, 100)
"""

import os
import socket
import subprocess
import sys
import time
import threading
import traceback
from datetime import datetime
from typing import Optional, Tuple

import cv2
import numpy as np

# 环境适配：原版 ADB_PATH = r"<data_drive>:/<emulator_root>\shell\adb.exe"（本机在 F 盘）
ADB_PATH = os.environ.get("MUMU_ADB", r"<data_drive>:/<apps_root>\YXArkNights-12.0\shell\adb.exe")

# MuMu12 默认 adb 端口与原版 7555 不同时的覆盖入口
_DEVICE_ADDR_ENV = os.environ.get("MUMU_ADB_ADDR", "")

MINITOUCH_PORT = 1111  # 原版固定端口

# 轻量日志与 error_record 替身（原版 from log import log_manager as log）
try:
    from log import log_manager as log  # type: ignore  # 同构环境（舟项目内）直接用
except ImportError:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger("simulator")  # .debug/.info/.warning/.error 与 log_manager 兼容


def error_record(e):
    """舟项目 tool.error_record 的轻量替身：记录异常与最深层回溯位置。"""
    tb = e.__traceback__
    if tb:
        frame = traceback.extract_tb(tb)[-1]
        log.error(f"{e} (at {frame.filename}:{frame.lineno})")
    else:
        log.error(str(e))


# RLock：click/swipe 失败重连路径会在持锁状态下经 ensure_connected→start 再次取锁
LOCK = threading.RLock()


class Simulator:
    """ADB + minitouch 长驻会话。构造不连接，首用时惰性拉起，close 只清自身。"""

    def __init__(self, adb_path=ADB_PATH, type="mumu", resource_dir=".", device_addr: str = ""):
        """
        参数:
            adb_path (str): ADB可执行文件路径。
            type (str): 设备类型，默认"mumu"（保留原版参数）。
            resource_dir (str): minitouch 二进制所在根目录（<resource_dir>/minitouch/<abi>/minitouch）。
            device_addr (str): 设备地址，如 127.0.0.1:16384；缺省回落 7555（原版默认）。
        """
        self.adb_path = adb_path
        self.device_addr = device_addr or _DEVICE_ADDR_ENV
        self.type = type
        self.resource_dir = resource_dir

        # 运行时状态（工具模式：构造期一律不启动任何服务）
        self._connected = False
        self.screen_size = None          # (w, h) 竖屏物理分辨率
        self.orientation = 0             # 0/1/2/3（SurfaceOrientation）
        self.minitouch_proc = None       # 常驻 minitouch 子进程
        self._mt_sock: Optional[socket.socket] = None  # 长连接 socket
        self._mt_ready = False           # minitouch push/forward 已就绪
        self._setup_resource_dir()

    # ---------- 生命周期 ----------

    def _setup_resource_dir(self):
        """设置资源目录结构（原版保留）"""
        minitouch_dir = os.path.join(self.resource_dir, "minitouch")
        os.makedirs(minitouch_dir, exist_ok=True)
        archs = ["x86_64", "x86", "arm64-v8a", "armeabi-v7a", "armeabi"]
        for arch in archs:
            os.makedirs(os.path.join(minitouch_dir, arch), exist_ok=True)

    def start(self) -> bool:
        """拉起会话（幂等，可反复调用）：adb connect + 探测 + minitouch 常驻。

        与原版 connect() 的差异仅生命周期语义：已就绪时直接返回；探测只在
        首次执行；不再「启动失败回退 input」以外的清理路径。
        """
        with LOCK:
            if self._session_alive():
                return True
            try:
                # adb server + 连接（offline 时由 ensure_connected 负责 reset_server 重试）
                subprocess.run([self.adb_path, "start-server"], check=True,
                               capture_output=True)
                if not self.device_addr:
                    self.device_addr = "127.0.0.1:7555"  # MUMU模拟器默认端口（原版值）
                log.info(f"连接设备: {self.device_addr}")
                subprocess.run([self.adb_path, "connect", self.device_addr], check=True,
                               capture_output=True)

                # 探测（只做一次并缓存）
                if self.screen_size is None:
                    result = subprocess.run(
                        [self.adb_path, "-s", self.device_addr, "shell", "wm size"],
                        capture_output=True, text=True, check=True)
                    size = result.stdout.strip().split()[-1].split("x")
                    self.screen_size = (int(size[0]), int(size[1]))
                    log.info(f"屏幕大小: {self.screen_size}")

                    result = subprocess.run(
                        [self.adb_path, "-s", self.device_addr,
                         "shell", "dumpsys input | grep SurfaceOrientation"],
                        capture_output=True, text=True, check=True)
                    self.orientation = 0
                    if "SurfaceOrientation" in result.stdout:
                        self.orientation = int(result.stdout.strip().split()[-1])
                    log.info(f"屏幕方向: {self.orientation * 90}度")

                # minitouch 常驻（进程活着就跳过 push/forward）
                if not self._mt_ready:
                    if not self._setup_minitouch():
                        log.warning("minitouch 不可用，click/swipe 将退回 input 命令")
                self._connected = True
                return True
            except subprocess.CalledProcessError as e:
                error_record(e)
                log.error(f"连接失败:{getattr(e, 'stderr', '')}")
                return False
            except Exception as e:
                error_record(e)
                log.error(f"连接失败: {e}")
                return False

    def _session_alive(self) -> bool:
        """会话是否已就绪（adb 连接 + minitouch 进程）。"""
        if not self._connected:
            return False
        if self.minitouch_proc is not None and self.minitouch_proc.poll() is not None:
            self._mt_ready = False  # minitouch 进程死了，下次 start 重新拉起
        return self._connected

    def _setup_minitouch(self) -> bool:
        """push minitouch（幂等覆盖）→ chmod → forward → 常驻进程。"""
        try:
            result = subprocess.run(
                [self.adb_path, "-s", self.device_addr, "shell", "getprop ro.product.cpu.abi"],
                capture_output=True, text=True, check=True)
            cpu_abi = result.stdout.strip()
            log.info(f"设备CPU架构: {cpu_abi}")

            minitouch_path = os.path.join(self.resource_dir, "minitouch", cpu_abi, "minitouch")
            if not os.path.exists(minitouch_path):
                log.error(f"找不到minitouch文件: {minitouch_path}")
                return False  # 工具模式下无 minitouch 则显式 False，调用方走 input 退路

            subprocess.run([self.adb_path, "-s", self.device_addr, "push",
                            minitouch_path, "/data/local/tmp/"], check=True, capture_output=True)
            subprocess.run([self.adb_path, "-s", self.device_addr, "shell",
                            "chmod 755 /data/local/tmp/minitouch"], check=True, capture_output=True)
            subprocess.run([self.adb_path, "-s", self.device_addr, "forward",
                            f"tcp:{MINITOUCH_PORT}", "localabstract:minitouch"],
                           check=True, capture_output=True)

            self.minitouch_proc = subprocess.Popen(
                [self.adb_path, "-s", self.device_addr, "shell", "/data/local/tmp/minitouch"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.4)  # 等 minitouch 监听
            if self.minitouch_proc.poll() is not None:
                self.minitouch_proc = None
                raise RuntimeError("minitouch启动失败")
            self._mt_ready = True
            log.info("minitouch启动成功")
            return True
        except Exception as e:
            error_record(e)
            log.error(f"设置minitouch失败: {e}")
            return False

    def ensure_connected(self) -> bool:
        """确认 adb 连接存活（echo 探测），断线自动重拉会话。"""
        if self._connected:
            try:
                result = subprocess.run(
                    [self.adb_path, "-s", self.device_addr, "shell", "echo", "test"],
                    capture_output=True, text=True, check=True)
                if result.stdout.strip() == "test":
                    return True
            except Exception:
                pass
            self._connected = False
            self._drop_mt_sock()
        return self.start()

    def reset_server(self):
        """device offline 时的恢复手段（唯一允许动 adb server 的地方）。"""
        try:
            log.info("尝试重新启动adb server")
            subprocess.run([self.adb_path, "kill-server"], check=True, capture_output=True)
            subprocess.run([self.adb_path, "start-server"], check=True, capture_output=True)
            return True
        except Exception as e:
            error_record(e)
            log.error(f"重启失败:{e}")
            return False

    def close(self):
        """结束会话：停 minitouch、关长连接、移除 forward。

        工具模式约定：**不杀 adb server、不删 /data/local/tmp/minitouch**——
        前者是共享资源（MaaEnd 可能正在用），后者留着下次启动免 push。
        可安全重复调用。
        """
        self._drop_mt_sock()
        if self.minitouch_proc is not None:
            try:
                self.minitouch_proc.terminate()
                self.minitouch_proc.wait(timeout=5)
            except Exception:
                pass
            self.minitouch_proc = None
        self._mt_ready = False
        if self.device_addr:
            try:
                subprocess.run([self.adb_path, "-s", self.device_addr, "forward",
                                "--remove", f"tcp:{MINITOUCH_PORT}"],
                               capture_output=True, timeout=10)
            except Exception:
                pass
        self._connected = False
        log.info("会话已关闭")

    def __enter__(self) -> "Simulator":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ---------- 内部 ----------

    def _drop_mt_sock(self):
        if self._mt_sock is not None:
            try:
                self._mt_sock.close()
            except Exception:
                pass
            self._mt_sock = None

    def _mt_socket(self) -> Optional[socket.socket]:
        """取长连接 socket；minitouch 未就绪时返回 None。"""
        if self._mt_sock is not None:
            return self._mt_sock
        if not (self._mt_ready and self.minitouch_proc
                and self.minitouch_proc.poll() is None):
            # minitouch 进程意外退出（如模拟器重启）：原位重拉一次（push 幂等，
            # 设备端文件未删，仅重走 push/chmod/forward/Popen）
            if not self._connected or not self._setup_minitouch():
                return None
        try:
            s = socket.create_connection(("127.0.0.1", MINITOUCH_PORT), timeout=3)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(0.5)
            try:
                s.recv(256)  # 丢弃 minitouch banner
            except OSError:
                pass
            s.settimeout(None)
            self._mt_sock = s
            return s
        except OSError as e:
            log.debug(f"minitouch socket 连接失败: {e}")
            return None

    def _convert_coordinates(self, x: int, y: int) -> Tuple[int, int]:
        """
        转换坐标，处理屏幕方向问题（原版逻辑不变）
        """
        if self.screen_size is None:
            return x, y

        if self.orientation == 0:  # 0度
            a, b = x, y
        elif self.orientation == 1:  # 90度
            a, b = self.screen_size[0] - y, x
        elif self.orientation == 2:  # 180度
            a, b = self.screen_size[1] - x, self.screen_size[0] - y
        elif self.orientation == 3:  # 270度
            a, b = y, self.screen_size[1] - x
        return int(a), int(b)

    def _ensure(self) -> bool:
        """操作前置保活：会话未起则惰性拉起。"""
        if self._session_alive():
            return True
        return self.start()

    # ---------- 能力 ----------

    def click(self, x, y, press_time=50):
        """
        点击指定坐标（游戏坐标，内部按 orientation 旋转）。
        长连接注入；socket 失效自动重建重试一次。
        :param press_time: 按下时间(毫秒)，默认50ms
        """
        try:
            x, y, press_time = int(x), int(y), int(press_time)
        except Exception as e:
            error_record(e)
            raise ValueError(f"坐标{x}或{y}或按下时间{press_time}不是数字")

        if not self._ensure():
            return False
        conv_x, conv_y = self._convert_coordinates(x, y)
        log.debug(f"点击坐标: ({x}, {y}) -> ({conv_x}, {conv_y}), 按下时间: {press_time}ms")

        with LOCK:
            payload_down = f"d 0 {conv_x} {conv_y} 50\nc\n".encode()
            for attempt in (0, 1):  # 失败重建 socket 再试一次
                s = self._mt_socket()
                if s is None:
                    break  # minitouch 不可用 → input 退路
                try:
                    s.sendall(payload_down)
                    time.sleep(press_time / 1000)
                    s.sendall("u 0\nc\n".encode())
                    time.sleep(0.01)
                    return True
                except OSError as e:
                    log.debug(f"minitouch 注入失败(attempt {attempt + 1}): {e}")
                    self._drop_mt_sock()
                    if not self.ensure_connected():
                        break
                    if attempt == 0:
                        continue

            # input 退路（原版回退行为；swipe 模拟长按）
            log.debug("minitouch未就绪，使用input命令")
            try:
                subprocess.run(
                    [self.adb_path, "-s", self.device_addr, "shell",
                     f"input swipe {conv_x} {conv_y} {conv_x} {conv_y} {press_time}"],
                    check=True, capture_output=True, timeout=15)
                return True
            except Exception as e:
                error_record(e)
                log.error(f"点击失败: {e}")
                return False

    def swipe(self, start_x: int, start_y: int, end_x: int, end_y: int, duration: int = 500) -> bool:
        """
        滑动屏幕（游戏坐标）。长连接注入，三次样条插值平滑（原版算法）。
        """
        try:
            int(start_x); int(start_y); int(end_x); int(end_y); int(duration)
        except Exception as e:
            error_record(e)
            raise ValueError(f"坐标{start_x}或{start_y}或{end_x}或{end_y}或持续时间{duration}不是数字")

        if not self._ensure():
            return False
        conv_start_x, conv_start_y = self._convert_coordinates(start_x, start_y)
        conv_end_x, conv_end_y = self._convert_coordinates(end_x, end_y)
        log.debug(f"滑动: ({start_x}, {start_y}) -> ({end_x}, {end_y}) => "
                  f"({conv_start_x}, {conv_start_y}) -> ({conv_end_x}, {conv_end_y}), 持续时间: {duration}ms")

        step_duration = 5  # 每5ms一步
        steps = max(duration // step_duration, 1)
        moves = []
        for i in range(steps):
            t = i / steps
            spline = t * t * t * (10 - 15 * t + 6 * t * t)  # 三次样条
            cur_x = int(conv_start_x + (conv_end_x - conv_start_x) * spline)
            cur_y = int(conv_start_y + (conv_end_y - conv_start_y) * spline)
            moves.append(f"m 0 {cur_x} {cur_y} 50\nc\n")

        with LOCK:
            for attempt in (0, 1):
                s = self._mt_socket()
                if s is None:
                    break
                try:
                    s.sendall(f"d 0 {conv_start_x} {conv_start_y} 50\nc\n".encode())
                    time.sleep(0.05)  # 起点停留50ms
                    # 分步发送保持节奏（合并会丢平滑度）
                    for mv in moves:
                        s.sendall(mv.encode())
                        time.sleep(step_duration / 1000)
                    s.sendall(b"u 0\nc\n")
                    time.sleep(0.01)
                    return True
                except OSError as e:
                    log.debug(f"minitouch 滑动失败(attempt {attempt + 1}): {e}")
                    self._drop_mt_sock()
                    if not self.ensure_connected():
                        break
                    if attempt == 0:
                        continue

            log.debug("minitouch未就绪，使用input命令")
            try:
                subprocess.run(
                    [self.adb_path, "-s", self.device_addr, "shell",
                     f"input swipe {conv_start_x} {conv_start_y} {conv_end_x} {conv_end_y} {duration}"],
                    check=True, capture_output=True, timeout=15)
                return True
            except Exception as e:
                error_record(e)
                log.error(f"滑动失败: {e}")
                return False

    def screenshot(self, retries: int = 3) -> Optional[np.ndarray]:
        """
        截图（原版逻辑不变：老式 adb shell screencap 通道 + \\r\\n 修复）。
        :return: BGR ndarray 或 None
        """
        log.debug("开始截图")
        if not self._ensure():
            return None

        for i in range(retries):
            try:
                result = subprocess.run(
                    [self.adb_path, "-s", self.device_addr, "shell", "screencap", "-p"],
                    capture_output=True, check=True)

                screen_bytes = result.stdout.replace(b'\r\n', b'\n')
                img_array = np.frombuffer(screen_bytes, np.uint8)
                img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)

                if img is not None:
                    log.debug("截图成功")
                    return img
                else:
                    log.warning(f"截图解码失败，重试 {i + 1}/{retries}")
            except subprocess.CalledProcessError as e:
                error_record(e)
                log.error(f"截图失败: {e}")
                if i < retries - 1:
                    time.sleep(1)
                continue
            except Exception as e:
                error_record(e)
                log.error(f"截图时发生未知错误: {e}")
                if i < retries - 1:
                    time.sleep(1)
                continue

        log.error("截图失败，已达到最大重试次数")
        return None
