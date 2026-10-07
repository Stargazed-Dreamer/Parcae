# -*- coding: utf-8 -*-
"""批量驱动 ABDM 插件静默下载抖音视频（9222 调试浏览器，后端托管）。

用法:
    python batch_download.py <links文件> [--start N] [--dl-dir DIR] [--base URL]

links 文件每行一条，支持两种格式（可混用）:
  1. 分享原文/规整行:  卡了，人物没加载出来【白沚】 | https://v.douyin.com/xxxx/
     -> 命名: NN_作者_标题.mp4
  2. 纯链接行:         https://v.douyin.com/xxxx/
     -> 命名: NN_页面标题清理版.mp4

前置:
  - 后端(8766)在线，且已用 exec_python 跑过 tools/browser/start_debug_browser.py
  - ABDM 主程序在运行
"""
import json, time, re, os, sys, glob, argparse, urllib.request, websocket

EXT_ID = "bbobopahenonfdgjgaleledndnnfhooj"  # ABDM Browser Integration
LOG = "batch_log.txt"

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# ---------- 解析链接文件 ----------
def load_items(path):
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            m = re.search(r"https://v\.douyin\.com/[A-Za-z0-9_-]+", line)
            if not m:
                continue
            url = m.group(0)
            lm = re.match(r"(.+?)【(.+?)】\s*[|｜]", line)
            title, author = (lm.group(1).strip(), lm.group(2).strip()) if lm else (None, None)
            items.append({"num": len(items) + 1, "url": url, "title": title, "author": author})
    return items

def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n#]+', "_", s or "")
    return re.sub(r"\s+", " ", s).strip(" ._")[:60]

# ---------- CDP 基础 ----------
def get_pages(base):
    return [t for t in json.loads(urllib.request.urlopen(base + "/json").read().decode())
            if t["type"] == "page"]

def ws_connect(base, url_filter=None, target_id=None):
    if target_id:
        t = next(x for x in get_pages(base) if x["id"] == target_id)
    else:
        t = next(x for x in get_pages(base) if url_filter in x["url"])
    return websocket.create_connection(t["webSocketDebuggerUrl"], timeout=15,
                                       suppress_origin=True)

def ev(ws, expr, rid=1, timeout_s=20):
    ws.settimeout(timeout_s)
    ws.send(json.dumps({"id": rid, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "returnByValue": True}}))
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        m = json.loads(ws.recv())
        if m.get("id") == rid:
            return m.get("result", {}).get("result", {}).get("value")
    return None

def open_page(base, url):
    urllib.request.urlopen(urllib.request.Request(
        f"{base}/json/new?{url}", method="PUT")).read()
    time.sleep(2)

def close_page(base, target_id):
    try:
        urllib.request.urlopen(f"{base}/json/close/{target_id}").read()
    except Exception:
        pass

# ---------- 静默配置（直接写扩展 storage，绕过 UI） ----------
def ensure_silent(base):
    open_page(base, f"chrome-extension://{EXT_ID}/src/optionsui/option-ui.html")
    time.sleep(2)
    t = next(x for x in get_pages(base) if "option-ui" in x["url"])
    ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=10, suppress_origin=True)
    js = """(async () => {
      const cur = await chrome.storage.local.get(['silentAddDownload','silentStartDownload']);
      if (cur.silentAddDownload && cur.silentStartDownload) return 'already';
      await chrome.storage.local.set({silentAddDownload:true, silentStartDownload:true});
      return 'set';
    })()"""
    # 必须 awaitPromise=True（chrome.storage.set 是异步的）
    ws.send(json.dumps({"id": 9, "method": "Runtime.evaluate",
                        "params": {"expression": js, "returnByValue": True, "awaitPromise": True}}))
    while True:
        m = json.loads(ws.recv())
        if m.get("id") == 9:
            r = m["result"]["result"].get("value"); break
    ws.close()
    close_page(base, t["id"])
    log(f"静默配置: {r}")
    return r in ("already", "set")

# ---------- 下载 ----------
def cleanup_tabs(base):
    """只保留一个非抖音页标签页（复用它导航），其余关闭，避免标签堆积。"""
    pages = get_pages(base)
    keep = next((p for p in pages if "douyin" not in p["url"]
                 and not p["url"].startswith("chrome-extension://")), None)
    if keep is None:
        open_page(base, "about:blank")
        keep = next(p for p in get_pages(base) if "about:blank" in p["url"])
    for p in pages:
        if p["id"] != keep["id"]:
            close_page(base, p["id"])
    time.sleep(1)
    return keep

def newest_mp4(dl_dir, since_ts):
    cands = [(os.path.getmtime(p), p) for p in glob.glob(os.path.join(dl_dir, "*.mp4"))
             if os.path.getmtime(p) >= since_ts]
    return max(cands)[1] if cands else None

def wait_download(dl_dir, since_ts, timeout_s=120):
    """等新 mp4 出现且体积 4 秒无增长（ABDM 分段下载最后合成）。"""
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        f = newest_mp4(dl_dir, since_ts)
        if f:
            last = f
            s1 = os.path.getsize(last)
            time.sleep(4)
            if os.path.getsize(last) == s1 and s1 > 100_000:
                return last, s1
        else:
            time.sleep(3)
    return (last, os.path.getsize(last)) if last else (None, 0)

CLICK_JS = """(() => {
  const header = document.querySelector('.abdm-media-header');
  if (header) header.click();
  const items = [...document.querySelectorAll('.abdm-media-item')];
  if (!items.length) return 'NO_ITEMS';
  const parseMB = (t) => { const m = t.match(/([0-9.]+)\\s*(MB|KB|GB)/i);
    if (!m) return 0; const v = parseFloat(m[1]);
    return m[2].toUpperCase()==='KB' ? v/1024 : m[2].toUpperCase()==='GB' ? v*1024 : v; };
  const vids = items.filter(it => /mp4/i.test(it.textContent));
  const pool = vids.length ? vids : items;
  pool.sort((a,b) => parseMB(b.textContent) - parseMB(a.textContent));
  pool[0].click();
  return 'CLICKED: ' + pool[0].textContent.trim().slice(0,60);
})()"""

def process(base, dl_dir, it, keep_id):
    log(f"=== [{it['num']}] {it['author'] or '?'} | {(it['title'] or it['url'])[:30]}")
    ws = ws_connect(base, target_id=keep_id)
    ev(ws, f"location.href = '{it['url']}'")
    ws.close()
    time.sleep(5)

    got = 0
    for _ in range(10):  # 最多 ~40s 等横幅
        time.sleep(4)
        try:
            ws = ws_connect(base, target_id=keep_id)
            state = ev(ws, "JSON.stringify({items: document.querySelectorAll('.abdm-media-item').length, title: document.title})",
                       timeout_s=8)
            ws.close()
            if state and json.loads(state).get("items", 0) > 0:
                got = json.loads(state)["items"]
                break
        except Exception as e:
            log(f"  等待异常: {e}")
    if not got:
        log("  !! 未检测到媒体条目")
        return None

    since = time.time() - 2
    ws = ws_connect(base, target_id=keep_id)
    click = ev(ws, CLICK_JS, timeout_s=10)
    ws.close()
    log(f"  {str(click)[:70]}")
    if not click or "CLICKED" not in str(click):
        return None

    path, size = wait_download(dl_dir, since)
    if not path:
        log("  !! 超时未见新文件")
        return None
    if it["title"] and it["author"]:
        name = f"{it['num']:02d}_{safe_name(it['author'])}_{safe_name(it['title'])}.mp4"
    else:
        # 回退：用页面标题清理（去 #话题、后缀）
        ws = ws_connect(base, target_id=keep_id)
        ptitle = ev(ws, "document.title") or it["url"]
        ws.close()
        ptitle = re.sub(r" - 抖音.*$", "", ptitle)
        ptitle = re.sub(r"#\S+", "", ptitle).strip(" |＿ ")
        name = f"{it['num']:02d}_{safe_name(ptitle) or 'douyin_video'}.mp4"
    target = os.path.join(dl_dir, name)
    try:
        os.rename(path, target)
        log(f"  OK {size/1e6:.1f}MB -> {name}")
        return target
    except OSError as e:
        log(f"  !! 改名失败({e}): {os.path.basename(path)}")
        return path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("links_file")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--dl-dir", default=r"<data_drive>:/<system_data_root><data_drive>:/Downloads")
    ap.add_argument("--base", default="http://127.0.0.1:9222")
    args = ap.parse_args()

    items = load_items(args.links_file)
    log(f"共 {len(items)} 条，从第 {args.start} 条开始，下载目录 {args.dl_dir}")
    if not ensure_silent(args.base):
        log("!! 静默配置失败，中止（否则每条都会弹窗）")
        return
    keep = cleanup_tabs(args.base)
    ok = fail = 0
    for it in items:
        if it["num"] < args.start:
            continue
        try:
            if process(args.base, args.dl_dir, it, keep["id"]):
                ok += 1
            else:
                fail += 1
        except Exception as e:
            log(f"  !! 异常: {e}")
            fail += 1
    log(f"完成: 成功 {ok} / 失败 {fail}")
    log("收尾: 运行 close_abdm_popups.py 关闭 ABDM 进度小窗")

if __name__ == "__main__":
    main()
