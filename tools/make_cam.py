#!/usr/bin/env python3
"""丸窓カム生成: 写真1枚から「呼吸するようにゆらぐ」円形の講師映像を作る。

    python3 tools/make_cam.py <写真のパス>

- 中央を正方形に切り出し → ゆっくり拡大縮小（呼吸）+ わずかな上下ゆれ
- 円形切り抜き + 金の縁取り（スライドのデザインと統一）
- 5秒のシームレスループ / アルファ付き mov → assets/cam.mov
- assemble.py が自動検出して左下に重ねる

将来AI生成版（fal）に差し替えても、cam.mov を置き換えるだけで済む。
"""
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
FPS = 30
DUR_SEC = 5
SIZE = 480          # cam.movの解像度（表示時は380pxに縮小される）
RING_GOLD = "if(between(hypot(X-W/2,Y-H/2),W/2-10,W/2-3)"


def main(photo: str) -> None:
    src = Path(photo).expanduser()
    if not src.exists():
        print(f"ERROR: 写真が見つかりません: {src}", file=sys.stderr)
        sys.exit(1)

    out = BASE / "assets" / "cam.mov"
    frames = FPS * DUR_SEC  # 150フレーム。sin周期を一致させてシームレスループにする

    # 1) 大きめに拡大してからzoompan（ガタつき防止）
    # 2) 呼吸: zoomを1周期のsinで往復 / わずかな縦ゆれ
    # 3) 円形マスク + 金の縁（rgb 200,168,106）
    vf = (
        "scale=1440:1440:force_original_aspect_ratio=increase,"
        "crop=1440:1440,"
        f"zoompan=z='1.06+0.025*sin(2*PI*on/{frames})'"
        f":x='iw/2-(iw/zoom/2)'"
        f":y='ih/2-(ih/zoom/2)+6*sin(4*PI*on/{frames})'"
        f":d={frames}:s={SIZE}x{SIZE}:fps={FPS},"
        "format=rgba,"
        "geq="
        f"r='{RING_GOLD},200,r(X,Y))':"
        f"g='{RING_GOLD},168,g(X,Y))':"
        f"b='{RING_GOLD},106,b(X,Y))':"
        "a='if(lte(hypot(X-W/2,Y-H/2),W/2-3),255,0)'"
    )

    print(f"丸窓カム生成中: {src.name} → {out.relative_to(BASE)}")
    r = subprocess.run(
        ["ffmpeg", "-y", "-loop", "1", "-i", str(src),
         "-vf", vf, "-frames:v", str(frames),
         "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
         str(out)],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        print(f"ERROR: 生成失敗\n{r.stderr[-600:]}", file=sys.stderr)
        sys.exit(1)

    size_mb = out.stat().st_size / 1024 / 1024
    print(f"完成: {DUR_SEC}秒ループ / {SIZE}x{SIZE} / {size_mb:.1f}MB")
    print("次回の assemble.py 実行時から自動で左下に重なります")
    print("プレビュー確認: open assets/cam.mov")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: make_cam.py <写真のパス>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
