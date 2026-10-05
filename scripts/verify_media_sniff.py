"""嗅探模块的自测: 候选打分 + 音频魔数识别。

用**真实酷我响应**的形状做输入 —— 包括那个必须被排除的客户端安装包地址,
否则"嗅探"会把 APK 当歌曲下下来。
"""

import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from smartcrawler.plugins.builtin._media_sniff import (  # noqa: E402
    _inner_urls,
    find_candidates,
    looks_like_audio,
)

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


class Rec:
    """最小化的 NetworkRecord 替身。"""

    def __init__(self, url: str, body) -> None:
        self.url = url
        self.body_json = body
        self.body_text = ""


#: 按实测酷我响应的真实形状构造
RECORDS = [
    # 搜索接口: 只有 rid, 没有任何音频地址
    Rec("https://kuwo.cn/openapi/v1/www/search/searchMusicBykeyWord", {
        "code": 200,
        "data": {"total": "328", "list": [
            {"musicrid": "MUSIC_4589227", "rid": 4589227, "name": "琵琶曲",
             "artist": "郑浩&冰洁", "album": "琵琶曲", "albumpic": "",
             "duration": 226, "songTimeMinutes": "03:46"},
        ]},
    }),
    # 客户端安装包(必须排除) —— 实测整页唯一的 url 字段就是这个
    Rec("https://mobilebasedata.kuwo.cn/api/sl/web/generate_url", {
        "code": 200,
        "data": {"url": "https://pkgdown.kuwo.cn/9879e233/mbox/"
                         "kwplayerautolite_C_APK_guanwang_lite.apk"},
    }),
    # 假设某站确实在接口里给了地址: 应能识别, 且解出签名跳转里的内层地址
    Rec("https://api.example.com/api/song/url", {
        "data": {
            "playUrl": "https://cdn.example.com/a/b/song.mp3?sign=abc",
            "audioUrl": "https://proxy.example.com/get?file="
                        "https%3A%2F%2Fcdn2.example.com%2Freal.flac",
            "pic": "https://cdn.example.com/cover.jpg",
            "lyric": "https://cdn.example.com/lrc/1.lrc",
        },
    }),
]


def main() -> int:
    print("=== 1) 候选发现与打分 ===")
    cands = find_candidates(RECORDS)
    urls = {c.url for c in cands}
    for c in cands:
        print(f"    score={c.score:<4} field={c.field!r}")
        print(f"        {c.url[:104]}")
        print(f"        {c.reason}")

    check(any("song.mp3" in u for u in urls), "**找到 song.mp3 候选**")
    check(any("real.flac" in u for u in urls),
          "**解出了签名跳转里的内层地址**(否则只拿到一层包装)")
    check(not any("kwplayerautolite" in u for u in urls),
          "**排除了客户端安装包(.apk)**")
    check(not any("cover.jpg" in u for u in urls), "排除了封面图")
    check(not any(u.endswith(".lrc") for u in urls), "排除了歌词文件")
    check(not any("searchMusicBykeyWord" in u for u in urls),
          "没有把接口自身的 URL 当媒体地址")

    print("\n=== 2) 内层地址解出 ===")
    inner = _inner_urls("https://proxy.example.com/get?file="
                        "https%3A%2F%2Fcdn2.example.com%2Freal.flac")
    print(f"    {inner}")
    check(inner == ["https://cdn2.example.com/real.flac"], "百分号编码的内层地址被正确解码")

    print("\n=== 3) 音频魔数识别 ===")
    cases = [
        (b"ID3\x04\x00\x00\x00\x00\x00\x00", "mp3", True),
        (b"\xff\xfb\x90\x00", "mp3", True),
        (b"\xff\xf3\x80\x00", "mp3", True),
        (b"fLaC\x00\x00\x00\x22", "flac", True),
        (b"OggS\x00\x02", "ogg", True),
        (b"RIFF\x00\x00\x00\x00WAVE", "wav/riff", True),
        (b"\x00\x00\x00\x1cftypM4A ", "mp4/m4a", True),
        (b"PK\x03\x04\x14\x00", None, False),        # APK/ZIP
        (b"<!DOCTYPE html>", None, False),           # HTML 错误页
        (b'{"code":200}', None, False),              # JSON
        (b"\x89PNG\r\n\x1a\n", None, False),         # 图片
        (b"", None, False),
    ]
    for head, expect, ok_flag in cases:
        got = looks_like_audio(head)
        label = f"{head[:8].hex(' ') or '(空)'} -> {got!r}"
        check((got is not None) == ok_flag, label, f"期望 {'音频' if ok_flag else '非音频'}")

    print("\n" + "=" * 66)
    if failures:
        print(f"嗅探模块自测: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("嗅探模块自测: 通过 ✓")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
