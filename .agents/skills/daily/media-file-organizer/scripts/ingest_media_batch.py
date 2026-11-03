#!/usr/bin/env python3
"""新批次入库：plan 只读生成映射表，run 按表解压/移动并校验。

适用两类来源：
  flat  : 一层 .7z（双层加密 或 单层直出），文件名即角色版本；目标名沿用包名
  dated : 按日期分的子目录，内含 zip（密码保护，包内 .exe 实为 mp4）+ 直出 mp4 + 说明 txt

用法:
  python ingest_media_batch.py plan --source "<data_drive>:/<media_inbox>\\10月补1" \
      --target "<data_drive>:/<bilibili_videos>作品\\2026.10月" --style flat --out temp/plan_oct_supp.csv
  python ingest_media_batch.py plan --source "<data_drive>:/<media_inbox>\\new2" \
      --target "<data_drive>:/<bilibili_videos>作品\\2026.10月" --style dated --out temp/plan_new2.csv

  set BZ_7Z_PW=... & set BZ_ZIP_PW=... & python ingest_media_batch.py run --mapping temp/plan_new2.csv

plan 生成的 CSV 必须人工复核 target 列（尤其 zip 名与包内名不一致的行）后再 run。
run 的纪律：全部预检通过才动手；逐条执行 + 落盘校验；目标已存在即停；不覆盖。
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SEVEN = r"C:\Program Files\7-Zip\7z.exe"
MEDIA_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi"}
ARCH_EXTS = {".7z", ".z7", ".zip", ".rar"}
DEFAULT_STAGE = r"<data_drive>:/<bilibili_videos>作品_extract_tmp"

DATE_PREFIX = re.compile(r"^\s*(\d{1,2})-(\d{1,2})")
DATE_ANY = re.compile(r"(\d{1,2})-(\d{1,2})")
COMPACT_DATE = re.compile(r"^(\d{2})(\d{2})(?=\D)")
TIME_SUFFIX = re.compile(r"_20\d{2}\.\d{1,2}\.\d{1,2}-\d{1,2}\.\d{1,2}\.\d{1,2}$")
PASSWORD_TOKEN = re.compile(r"(密码|password)\s*\d*", re.I)
TRAILING_DIGITS = re.compile(r"\d+$")
VERSION_TOKENS = [("无黑条版", "无黑条版"), ("平台版", "平台版"), ("平台", "平台版"),
                  ("原画", "原画"), ("差分", "差分")]

CSV_FIELDS = ["source", "kind", "container", "entry", "size", "target",
              "date", "character", "version", "note"]


def decode(b: bytes | None) -> str:
    return (b or b"").decode("gbk", errors="replace")


def run7z(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run([SEVEN, *args], capture_output=True, timeout=timeout)


def list7z(archive: Path, password: str | None = None) -> list[tuple[str, int]]:
    args = ["l", "-slt"]
    if password:
        args.append(f"-p{password}")
    args.append(str(archive))
    p = run7z(args)
    if p.returncode != 0:
        raise RuntimeError(f"列目录失败 rc={p.returncode}: {decode(p.stderr)[-200:]}")
    text = decode(p.stdout)
    if "----------" not in text:
        raise RuntimeError("列目录输出缺少分隔符")
    body = text.split("----------", 1)[1]
    entries, cur = [], None
    for line in body.splitlines():
        m = re.match(r"^(Path|Size) = (.*)$", line.strip())
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k == "Path":
            if cur:
                entries.append(cur)
            cur = (v, 0)
        elif cur:
            cur = (cur[0], int(v))
    if cur:
        entries.append(cur)
    return [(n.rstrip("\r"), s) for n, s in entries]


def media_like(name: str) -> bool:
    return Path(name).suffix.lower() in MEDIA_EXTS | {".exe"}


def kind_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in MEDIA_EXTS:
        return "media"
    if ext == ".exe":
        return "exe"
    if ext in ARCH_EXTS:
        return "archive"
    return "other"


def parse_name(stem: str, fallback_date: str) -> tuple[str, str, str | None]:
    s = TIME_SUFFIX.sub("", stem)
    date = fallback_date
    m = DATE_PREFIX.match(s)
    if m:
        date = f"{int(m.group(1))}-{int(m.group(2))}"
        s = s[m.end():]
    else:
        m = COMPACT_DATE.match(s)
        if m:
            date = f"{int(m.group(1))}-{int(m.group(2))}"
            s = s[m.end():]
    s = DATE_ANY.sub("", s)
    s = PASSWORD_TOKEN.sub("", s)
    version = None
    for token, ver in VERSION_TOKENS:
        if token in s:
            version = ver
            s = s.replace(token, "")
            break
    s = s.strip(" _-")
    s = TRAILING_DIGITS.sub("", s).strip(" _-")
    return date, s, version


def collect_units(source: Path, style: str, sevenz_pw: str | None) -> list[dict]:
    units = []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        rel = str(path.relative_to(source))
        if ext in (".7z", ".z7"):
            entries = list7z(path)
            if len(entries) != 1:
                units.append({"source": rel, "kind": "7z", "container": "error",
                              "entry": "", "size": 0, "note": f"外层条目数={len(entries)}"})
                continue
            e_name, e_size = entries[0]
            ek = kind_of(e_name)
            if ek in ("media", "exe"):
                units.append({"source": rel, "kind": "7z", "container": "single7z",
                              "entry": e_name, "size": e_size, "note": ""})
            elif ek == "archive":
                units.append({"source": rel, "kind": "7z", "container": "double7z",
                              "entry": "", "size": 0, "note": ""})
            else:
                units.append({"source": rel, "kind": "7z", "container": "error",
                              "entry": e_name, "size": e_size, "note": "外层条目类型未知"})
        elif ext == ".zip":
            entries = [e for e in list7z(path) if not e[0].lower().endswith(".txt")]
            if len(entries) != 1:
                units.append({"source": rel, "kind": "zip", "container": "error",
                              "entry": "", "size": 0, "note": f"非txt条目数={len(entries)}"})
                continue
            e_name, e_size = entries[0]
            if not media_like(e_name):
                units.append({"source": rel, "kind": "zip", "container": "error",
                              "entry": e_name, "size": e_size, "note": "包内不是媒体/exe"})
            else:
                units.append({"source": rel, "kind": "zip", "container": "zip",
                              "entry": e_name, "size": e_size, "note": ""})
        elif ext in MEDIA_EXTS:
            units.append({"source": rel, "kind": "media", "container": "direct",
                          "entry": path.name, "size": path.stat().st_size, "note": ""})
    return units


def plan(source: Path, target: Path, style: str, sevenz_pw: str | None) -> list[dict]:
    units = collect_units(source, style, sevenz_pw)
    for u in units:
        src = source / u["source"]
        path = src
        if style == "flat":
            stem = path.stem
            u.update({"target": stem + ".mp4", "date": "", "character": stem,
                      "version": "", "note": (u["note"] + "; " if u["note"] else "") + "flat:沿用包名"})
            continue
        folder = path.parent.name
        fallback = folder if re.fullmatch(r"\d{1,2}-\d{1,2}", folder) else ""
        # 命名以包内名为准（zip/7z），否则用源文件名
        basis = u["entry"] if u["entry"] else path.name
        stem = Path(basis).stem
        date, char, version = parse_name(stem, fallback)
        default_version = "差分" if u["kind"] == "zip" else "原画"
        if u["kind"] == "7z" and u["container"] == "double7z":
            default_version = "差分"
        u.update({"date": date, "character": char, "version": version or default_version,
                  "target": ""})
        # zip 名与包内名不一致时提示
        if u["kind"] == "zip" and u["entry"]:
            _, zip_char, _ = parse_name(path.stem, fallback)
            entry_char = char
            if zip_char and entry_char and zip_char != entry_char:
                if zip_char in entry_char or entry_char in zip_char:
                    longer = zip_char if len(zip_char) > len(entry_char) else entry_char
                    u["character"] = longer
                    u["note"] += f"采用更完整名 {longer}（zip名={zip_char}, 包内={entry_char}）; "
                else:
                    u["note"] += f"zip名={zip_char} 与包内={entry_char} 不符，按包内; "

    # 同目录内：空角色继承；包含关系的短名归一
    groups: dict[str, list[dict]] = {}
    for u in units:
        groups.setdefault(str((source / u["source"]).parent), []).append(u)
    for _, rows in groups.items():
        chars = [r["character"] for r in rows if r.get("character")]
        if len(set(chars)) == 1:
            for r in rows:
                if not r.get("character"):
                    r["character"] = chars[0]
        for r in rows:
            c = r.get("character", "")
            for other in chars:
                if c and other and c != other and c in other:
                    r["character"] = other
                    r["note"] += f"角色名归一为 {other}; "
                    break

    for u in units:
        if style == "flat":
            continue
        char = u.get("character") or "未命名"
        if char == "未命名":
            u["note"] += "角色名缺失; "
        u["target"] = f"{u['date']} {char} {u['version']}.mp4"

    # 目标冲突检查
    seen = {}
    for u in units:
        key = u["target"].casefold()
        if key in seen:
            u["note"] += "与映射表内其它行重名; "
        seen[key] = True
        if (target / u["target"]).exists():
            u["note"] += "目标已存在; "
    return units


def write_plan(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})


def ftyp_ok(path: Path) -> bool:
    with path.open("rb") as f:
        return b"ftyp" in f.read(64)[:32]


def safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]', "_", s)


def execute(mapping: Path, target_dir: Path, stage_root: Path,
            sevenz_pw: str, zip_pw: str) -> int:
    rows = list(csv.DictReader(mapping.open(encoding="utf-8-sig", newline="")))
    if not rows:
        print("[BAD] 映射表为空")
        return 2
    # 预检
    problems = []
    targets = set()
    for i, r in enumerate(rows, 2):
        src = Path(r["source"])
        if not src.is_absolute():
            src = mapping.parent / src  # 允许相对映射表；但默认写出绝对路径
        if not src.exists():
            problems.append(f"第{i}行 源不存在: {r['source']}")
            continue
        tgt = target_dir / r["target"]
        if tgt.exists():
            problems.append(f"第{i}行 目标已存在: {tgt.name}")
        key = r["target"].casefold()
        if key in targets:
            problems.append(f"第{i}行 映射表内重名: {r['target']}")
        targets.add(key)
        if r["container"] == "error":
            problems.append(f"第{i}行 规划错误: {r['note']}")
    if problems:
        for p in problems:
            print(f"[BAD] {p}")
        print("[BAD] 预检未通过，未做任何操作")
        return 1

    stage_root.mkdir(exist_ok=True)
    done = 0
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        src = Path(r["source"])
        if not src.is_absolute():
            src = mapping.parent / src
        tgt = target_dir / r["target"]
        container = r["container"]
        stage = stage_root / f"{i:04d}_{safe(src.stem)}"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        try:
            if container == "direct":
                if not ftyp_ok(src):
                    raise RuntimeError("源文件不是有效 MP4")
                os.rename(src, tgt)
            elif container == "zip":
                out_dir = stage / "zip"
                out_dir.mkdir()
                p = run7z(["x", str(src), f"-p{zip_pw}", f"-o{out_dir}", "-y", "-bso0", "-bsp0"])
                if p.returncode != 0:
                    raise RuntimeError(f"解 zip 失败 rc={p.returncode}: {decode(p.stderr)[-200:]}")
                want = Path(r["entry"]).name.rstrip("\r")
                files = [x for x in out_dir.rglob("*") if x.is_file() and x.name == want]
                if len(files) != 1:
                    raise RuntimeError(f"未找到包内文件 {want}（找到 {len(files)} 个）")
                if not ftyp_ok(files[0]):
                    raise RuntimeError("解出文件不是有效 MP4")
                os.rename(files[0], tgt)
            elif container in ("single7z", "double7z"):
                out_dir = stage / "outer"
                out_dir.mkdir()
                p = run7z(["x", str(src), f"-o{out_dir}", "-y", "-bso0", "-bsp0"])
                if p.returncode != 0:
                    raise RuntimeError(f"解外层失败 rc={p.returncode}: {decode(p.stderr)[-200:]}")
                inners = [x for x in out_dir.rglob("*") if x.is_file()]
                if len(inners) != 1:
                    raise RuntimeError(f"外层内文件数={len(inners)}")
                payload = inners[0]
                if container == "single7z":
                    media = payload
                else:
                    entries = list7z(payload, sevenz_pw)
                    media_entries = [(n, s) for n, s in entries if Path(n).suffix.lower() in MEDIA_EXTS]
                    if len(media_entries) != 1 or len(media_entries) != len(entries):
                        raise RuntimeError(f"内层条目异常: {entries}")
                    in_dir = stage / "inner"
                    in_dir.mkdir()
                    p = run7z(["x", str(payload), f"-p{sevenz_pw}", f"-o{in_dir}", "-y", "-bso0", "-bsp0"])
                    if p.returncode != 0:
                        raise RuntimeError(f"解内层失败 rc={p.returncode}: {decode(p.stderr)[-200:]}")
                    got = [x for x in in_dir.rglob("*") if x.is_file()]
                    if len(got) != 1:
                        raise RuntimeError(f"内层解出文件数={len(got)}")
                    media = got[0]
                    if media.stat().st_size != media_entries[0][1]:
                        raise RuntimeError("内层大小与声明不符")
                if not ftyp_ok(media):
                    raise RuntimeError("解出文件不是有效 MP4")
                os.rename(media, tgt)
            else:
                raise RuntimeError(f"未知 container: {container}")

            if not tgt.exists() or tgt.stat().st_size == 0:
                raise RuntimeError("落盘校验失败")
            done += 1
            print(f"[OK] {i}/{len(rows)} {src.name} -> {tgt.name}")
        except Exception as exc:
            print(f"[BAD] {src.name}: {exc}")
            return 1
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    print(f"[INFO] done={done}/{len(rows)} elapsed={time.time()-t0:.1f}s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="新批次入库（plan / run）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("plan", help="只读扫描，生成映射表")
    p1.add_argument("--source", required=True)
    p1.add_argument("--target", required=True)
    p1.add_argument("--style", choices=["flat", "dated"], required=True)
    p1.add_argument("--out", required=True)
    p1.add_argument("--sevenz-password", default=os.environ.get("BZ_7Z_PW"))

    p2 = sub.add_parser("run", help="按映射表执行")
    p2.add_argument("--mapping", required=True)
    p2.add_argument("--target", required=True)
    p2.add_argument("--stage", default=DEFAULT_STAGE)
    p2.add_argument("--sevenz-password", default=os.environ.get("BZ_7Z_PW"))
    p2.add_argument("--zip-password", default=os.environ.get("BZ_ZIP_PW"))

    args = ap.parse_args()
    if args.cmd == "plan":
        source, target = Path(args.source).resolve(), Path(args.target).resolve()
        if not source.is_dir() or not target.is_dir():
            print("[BAD] source 或 target 目录不存在")
            return 2
        rows = plan(source, target, args.style, args.sevenz_password)
        # source 写成绝对路径，run 不受工作目录影响
        for r in rows:
            r["source"] = str(source / r["source"])
        write_plan(rows, Path(args.out).resolve())
        for r in rows:
            print(f"{r['container']:9} {r['source'].rsplit(chr(92),1)[-1]:40} -> {r['target']}"
                  + (f"  [{r['note']}]" if r["note"] else ""))
        print(f"[OK] {len(rows)} 行 -> {args.out}")
        return 0

    sevenz_pw = args.sevenz_password or ""
    zip_pw = args.zip_password or ""
    if not sevenz_pw and not zip_pw:
        print("[BAD] 需要密码：--sevenz-password / --zip-password 或环境变量")
        return 2
    return execute(Path(args.mapping).resolve(), Path(args.target).resolve(),
                   Path(args.stage).resolve(), sevenz_pw, zip_pw)


if __name__ == "__main__":
    sys.exit(main())
