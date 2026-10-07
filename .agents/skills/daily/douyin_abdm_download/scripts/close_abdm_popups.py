# -*- coding: utf-8 -*-
"""关闭 AB Download Manager 的多余弹窗（下载进度小窗 + 残留「添加下载」框）。

用法: python close_abdm_popups.py [--dry-run]
每个 ABDM 下载会弹一个 675x240 的进度小窗且全部叠在同一位置，批量下载后需清理。
"""
import ctypes, ctypes.wintypes as wt, argparse

user32 = ctypes.windll.user32
WM_CLOSE = 0x0010

def is_target(title):
    t = title or ""
    if ".mp4" in t and "douyin" in t.lower():   # 进度小窗（标题=下载中文件名）
        return True
    if t == "添加下载":                          # 残留确认框
        return True
    return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, l):
        if user32.IsWindowVisible(h):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(h, buf, 256)
            if is_target(buf.value):
                found.append((h, buf.value[:60]))
        return True

    user32.EnumWindows(cb, 0)
    if not found:
        print("无 ABDM 弹窗")
        return
    for h, t in found:
        print(("DRY " if args.dry_run else "CLOSE ") + f"hwnd={h} {t}")
        if not args.dry_run:
            user32.PostMessageW(h, WM_CLOSE, 0, 0)
    print(f"共 {len(found)} 个")

if __name__ == "__main__":
    main()
