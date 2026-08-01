#!/usr/bin/env python3
"""segments.json からスライドHTMLを生成し、Chromeヘッドレスで1920x1080のPNGに撮影する。

出力: slides/lessonNN/segNN.html, segNN.png
検収: 生成枚数とPNGサイズ(1920x1080)を確認。不一致なら異常終了。
"""
import html
import json
import struct
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LOGO_TEXT = "LIGHTECH"
# AI音声を使った動画には、その旨を明記する（自主ルール）。
# assets/slide_config.json の "ai_notice" で文言を変えられるが、
# 空にすると警告が出る。黙って消せないようにしてある。
AI_NOTICE = "音声はAIで生成しています"


def load_config() -> dict:
    cfg_path = BASE / "assets" / "slide_config.json"
    if cfg_path.exists():
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    return {}


def render_html(seg: dict, lesson_title: str, logo: str, notice: str = AI_NOTICE) -> str:
    tpl_name = "slide_title.html" if seg["type"] == "title" else "slide_body.html"
    tpl = (BASE / "templates" / tpl_name).read_text(encoding="utf-8")
    bullets = "\n      ".join(f"<li>{html.escape(b)}</li>" for b in seg["bullets"])
    # 注記はフッターに繋げる。独立した要素にすると、回によっては
    # とびらと本文でレイアウトが違うぶん位置がずれたり重なったりする。
    footer = "　｜　".join(x for x in (lesson_title, notice) if x)
    return (
        tpl.replace("{{LOGO}}", html.escape(logo))
           .replace("{{HEADING}}", html.escape(seg["heading"]))
           .replace("{{BULLETS}}", bullets)
           .replace("{{FOOTER}}", html.escape(footer))
    )


def png_size(path: Path) -> tuple:
    """PNGヘッダから幅・高さを読む（追加ライブラリ不要）"""
    with open(path, "rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    w, h = struct.unpack(">II", head[16:24])
    return (w, h)


def screenshot(html_path: Path, png_path: Path) -> None:
    cmd = [
        CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--window-size=1920,1080",
        f"--screenshot={png_path}",
        f"file://{html_path}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if not png_path.exists():
        raise RuntimeError(f"スクショ失敗: {html_path.name}\n{result.stderr[-500:]}")


def main(lesson: str) -> None:
    seg_path = BASE / "build" / f"lesson{lesson}" / "segments.json"
    if not seg_path.exists():
        print(f"ERROR: 先に parse_script.py を実行してください（{seg_path} がない）", file=sys.stderr)
        sys.exit(1)

    data = json.loads(seg_path.read_text(encoding="utf-8"))
    cfg = load_config()
    logo = cfg.get("logo_text", LOGO_TEXT)
    notice = cfg.get("ai_notice", AI_NOTICE)

    out_dir = BASE / "slides" / f"lesson{lesson}"
    out_dir.mkdir(parents=True, exist_ok=True)

    errors = []
    for seg in data["segments"]:
        html_path = out_dir / f"{seg['id']}.html"
        png_path = out_dir / f"{seg['id']}.png"
        html_path.write_text(render_html(seg, data["title"], logo, notice), encoding="utf-8")
        screenshot(html_path, png_path)
        w, h = png_size(png_path)
        if (w, h) != (1920, 1080):
            errors.append(f"{png_path.name}: サイズ異常 {w}x{h}（期待 1920x1080）")

    n = len(data["segments"])
    pngs = sorted(out_dir.glob("seg*.png"))
    print(f"スライド生成完了: {n}枚 → {out_dir.relative_to(BASE)}")
    if len(pngs) != n:
        errors.append(f"枚数不一致: 期待{n}枚 / 実際{len(pngs)}枚")
    if errors:
        for e in errors:
            print(f"  ❌ {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  検収OK: 全{n}枚が1920x1080")
    if notice:
        print(f"  AI音声の注記: 「{notice}」を全{n}枚に表示")
    else:
        print("  ⚠️  AI音声の注記が空です。AI音声を使う動画には明記する取り決めです。",
              file=sys.stderr)
        print("     assets/slide_config.json の \"ai_notice\" を設定してください。",
              file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: make_slides.py <lesson番号 例:01>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
