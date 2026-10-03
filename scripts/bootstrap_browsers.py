"""
bootstrap_browsers.py —— 下载 Playwright 浏览器内核(带进度、断点续传、自动重试)。

**为什么需要这个脚本**: ``python -m playwright install`` 在部分网络环境下会静默
卡住 —— 它不打印任何进度, 出问题时无法区分"网速慢"和"已经死了"。这里复用官方
下载器的 **dry-run 输出**(唯一权威的 URL 与目标目录来源), 再用 httpx 自己实现
一个看得见进度、断了能续传的下载器。

用法::

    python scripts/bootstrap_browsers.py                # 下载 chromium(默认, 够用)
    python scripts/bootstrap_browsers.py --all          # 连 headless shell / ffmpeg 一起
    python scripts/bootstrap_browsers.py --browser firefox
    python scripts/bootstrap_browsers.py --list         # 只显示计划和当前状态
    python scripts/bootstrap_browsers.py --force        # 已安装也重新下载
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 先准备好可写临时目录再导入会写临时文件的库。
# prepare_temp_dir() 同时会把 PLAYWRIGHT_BROWSERS_PATH 指向项目内的 .browsers,
# 因此在它之后导入 playwright 才能拿到正确路径。
from smartcrawler.web.__main__ import ensure_browsers_path, prepare_temp_dir  # noqa: E402

prepare_temp_dir()

import httpx  # noqa: E402

CHUNK = 1 << 20  # 1 MiB
# 浏览器落地目录(项目内可写; 用户显式设置过 PLAYWRIGHT_BROWSERS_PATH 时以用户为准)
BROWSERS_ROOT = ensure_browsers_path()


def _force_utf8_stdout() -> None:
    """Windows 控制台默认是 GBK, 输出 █/░ 进度条会抛 UnicodeEncodeError。

    sys.stdout.reconfigure 是 3.7+ 的标准做法; 若失败(被重定向到不支持
    reconfigure 的对象)则退化为纯 ASCII 进度条, 由 _progress 内部判断。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def _supports_unicode_bar() -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "█░".encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


_UTF8_BAR = True


# dry-run 输出形如:
#   Chrome for Testing 153.0.8010.12 (playwright chromium v1243)
#     Install location:    C:\...\ms-playwright\chromium-1243
#     Download url:        https://cdn.playwright.dev/builds/cft/.../chrome-win64.zip
#   Download fallback 1:  https://...
_HEADER_RE = re.compile(r"^(?P<title>.+?)\s*\(playwright (?P<name>[\w-]+) v(?P<rev>\d+)\)\s*$")
_INSTALL_RE = re.compile(r"^\s*Install location:\s*(?P<path>.+?)\s*$")
_URL_RE = re.compile(r"^\s*Download (?:url|fallback \d+):\s*(?P<url>\S+)\s*$")


def dry_run(browsers: list[str]) -> list[dict]:
    """调用官方安装器的 dry-run, 解析出 (名称, 目录, 候选 URL 列表)。"""
    cmd = [sys.executable, "-m", "playwright", "install", *browsers, "--dry-run"]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise SystemExit(
            "无法获取下载计划, 请确认已安装 playwright:\n"
            f"  命令: {' '.join(cmd)}\n{(result.stderr or result.stdout or '').strip()}"
        )

    items: list[dict] = []
    current: dict | None = None
    for line in (result.stdout or "").splitlines():
        header = _HEADER_RE.match(line.strip())
        if header:
            current = {
                "name": header.group("name"),
                "revision": header.group("rev"),
                "title": header.group("title").strip(),
                "urls": [],
                "install_dir": None,
            }
            items.append(current)
            continue
        if current is None:
            continue
        install = _INSTALL_RE.match(line)
        if install:
            current["install_dir"] = Path(install.group("path"))
            continue
        url = _URL_RE.match(line)
        if url:
            current["urls"].append(url.group("url"))
    return [item for item in items if item["urls"] and item["install_dir"]]


def is_installed(item: dict) -> bool:
    """目录存在且有内容就视为已安装(Playwright 本身也用标记文件判断)。"""
    path: Path = item["install_dir"]
    if not path.exists():
        return False
    if (path / "INSTALLATION_COMPLETE").exists():
        return True
    try:
        return any(path.iterdir())
    except OSError:
        return False


def download(urls: list[str], target: Path, retries: int = 6) -> Path:
    """按候选 URL 依次尝试, 分块下载并打印进度; 支持断点续传。"""
    resume_from = target.stat().st_size if target.exists() else 0
    last_error = ""

    for url_index, url in enumerate(urls):
        attempt = 0
        while attempt < retries:
            attempt += 1
            headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(30.0, read=180.0, write=30.0, pool=30.0),
                    follow_redirects=True,
                    headers={"User-Agent": "SmartCrawler-bootstrap/1.0"},
                ) as client:
                    with client.stream("GET", url, headers=headers) as response:
                        if response.status_code == 416:  # 已下载完整
                            print("    文件已完整, 跳过下载")
                            return target
                        response.raise_for_status()
                        total = int(response.headers.get("Content-Length", 0)) + resume_from
                        started = time.perf_counter()
                        done = resume_from
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with target.open("ab" if resume_from else "wb") as handle:
                            for chunk in response.iter_bytes(CHUNK):
                                handle.write(chunk)
                                done += len(chunk)
                                _progress(done, total, done - resume_from, started)
                print()
                return target
            except (httpx.HTTPError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                print(f"\n    [!] 中断: {last_error}")
                resume_from = target.stat().st_size if target.exists() else 0
                if attempt >= retries:
                    break
                wait = min(2**attempt, 20)
                print(f"    {wait}s 后从 {resume_from / 1048576:.1f} MB 续传(第 {attempt}/{retries} 次)")
                time.sleep(wait)
        if url_index + 1 < len(urls):
            print(f"    换用备用地址重试: {urls[url_index + 1]}")

    raise SystemExit(f"下载失败(已尝试全部地址): {last_error}")


def _progress(done: int, total: int, transferred: int, started: float) -> None:
    elapsed = max(time.perf_counter() - started, 0.001)
    speed = transferred / elapsed / 1048576
    if total:
        ratio = min(done / total, 1.0)
        filled = int(28 * ratio)
        bar = ("█" * filled + "░" * (28 - filled)) if _UTF8_BAR else ("#" * filled + "-" * (28 - filled))
        sys.stdout.write(
            f"\r    [{bar}] {ratio * 100:5.1f}%  {done / 1048576:7.1f}/{total / 1048576:.1f} MB  {speed:5.1f} MB/s"
        )
    else:
        sys.stdout.write(f"\r    已下载 {done / 1048576:.1f} MB  {speed:5.1f} MB/s")
    sys.stdout.flush()


def _robust_rmtree(path: Path, retries: int = 5) -> None:
    """删除目录并重试。

    刚下载完的大文件常被杀软/文件索引器短暂持有, 此时 rmtree/mkdir 会抛
    ``PermissionError [WinError 5]`` —— 属于瞬时故障, 重试即可。
    """
    for attempt in range(retries):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if attempt == retries - 1:
                # 最后兜底: 逐个删除, 不因单个文件失败而整棵树放弃
                for child in sorted(path.rglob("*"), reverse=True):
                    try:
                        child.unlink() if child.is_file() else child.rmdir()
                    except OSError:
                        pass
                return
            print(f"    目录被占用({exc.__class__.__name__}), {attempt + 1}s 后重试删除…")
            time.sleep(attempt + 1)


def _robust_mkdir(path: Path, retries: int = 5) -> None:
    """创建目录并重试(同上, 应对瞬时占用)。"""
    for attempt in range(retries):
        try:
            path.mkdir(parents=True, exist_ok=True)
            return
        except FileExistsError:
            return
        except OSError:
            if attempt == retries - 1:
                raise
            time.sleep(attempt + 1)


def extract(zip_path: Path, install_dir: Path) -> None:
    """解压到 Playwright 期望的目录, 并写入安装完成标记。

    这里对解压本身也做重试: 杀软实时扫描新建目录时, ``makedirs`` 可能瞬时返回
    ``WinError 5``(实测在 Windows 上确实会偶发), 而重试一次就成功。
    """
    if install_dir.exists():
        _robust_rmtree(install_dir)
    _robust_mkdir(install_dir)

    for attempt in range(4):
        try:
            print(f"    解压到 {install_dir} …")
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(install_dir)
            break
        except (OSError, zipfile.BadZipFile) as exc:
            if attempt == 3:
                raise
            print(f"    解压被中断({exc.__class__.__name__}: {exc}), {attempt + 1}s 后重试…")
            time.sleep(attempt + 1)
            _robust_mkdir(install_dir)

    # Playwright 用这个文件判断"安装已完成", 缺失时会尝试重新下载
    (install_dir / "INSTALLATION_COMPLETE").write_text("", encoding="utf-8")


def _zip_is_complete(zip_path: Path) -> bool:
    """判断已存在的 zip 是否完整可解压(用于跳过重复下载)。"""
    if not zip_path.exists() or zip_path.stat().st_size == 0:
        return False
    try:
        with zipfile.ZipFile(zip_path) as archive:
            return archive.testzip() is None
    except (zipfile.BadZipFile, OSError):
        return False


def main() -> int:
    global _UTF8_BAR
    _force_utf8_stdout()
    _UTF8_BAR = _supports_unicode_bar()

    parser = argparse.ArgumentParser(description="下载 Playwright 浏览器内核(带进度/续传)")
    parser.add_argument("--all", action="store_true", help="下载全部内核(chromium + headless shell + ffmpeg 等)")
    parser.add_argument("--browser", action="append", default=None, help="指定内核名, 可重复: --browser firefox")
    parser.add_argument("--list", action="store_true", help="只显示计划与状态, 不下载")
    parser.add_argument("--force", action="store_true", help="已安装也重新下载")
    args = parser.parse_args()

    if args.browser:
        browsers = args.browser
    elif args.all:
        browsers = ["chromium", "chromium-headless-shell", "ffmpeg", "winldd"]
        if sys.platform != "win32":
            browsers.remove("winldd")
    else:
        browsers = ["chromium"]

    items = dry_run(browsers)

    print(f"临时下载目录: {prepare_temp_dir()}\n")
    todo: list[dict] = []
    for item in items:
        installed = is_installed(item)
        status = "已安装" if installed else "待下载"
        print(f"  · {item['name']:<26} {item['title']:<32} {status}")
        if not installed or args.force:
            todo.append(item)

    if args.list:
        return 0
    if not todo:
        print("\n全部内核均已就绪, 无需下载。")
        return 0

    for item in todo:
        print(f"\n>>> {item['title']}")
        zip_path = prepare_temp_dir() / f"{item['name']}-{item['revision']}.zip"
        # 上次下载可能已完成只差解压, 此时直接复用, 省掉几百 MB 流量
        if _zip_is_complete(zip_path):
            print(f"    复用已下载的完整安装包 ({zip_path.stat().st_size / 1048576:.1f} MB)")
        else:
            print(f"    {item['urls'][0]}")
            download(item["urls"], zip_path)
        extract(zip_path, item["install_dir"])
        zip_path.unlink(missing_ok=True)
        print(f"    ✓ {item['name']} 安装完成")

    print("\n全部完成。自检:")
    print(
        '  python -c "from playwright.sync_api import sync_playwright;p=sync_playwright().start();'
        "b=p.chromium.launch(headless=True);print('chromium', b.version);b.close();p.stop()\""
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
