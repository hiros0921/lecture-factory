#!/usr/bin/env python3
"""スライドPNG＋音声wavから講義動画mp4を組み立てる。

- 各セグメント: 音声の長さ + 0.8秒 だけスライドを表示（-t で明示区切り＝無限伸長の罠回避）
- 1920x1080 / H.264 / AAC / faststart（標準的な再生設定）
- assets/cam.mov があれば左下にループ重ね
- フルテロップ焼き込み（assets/video_config.json の "telop": false で無効化）
- 完成後、合計時間とサイズを報告
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import telop as telop_mod

BASE = Path(__file__).resolve().parent.parent
PAD_SEC = 0.8
FPS = 30


def run(cmd: list, timeout: int = 600) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"コマンド失敗: {' '.join(cmd[:6])}...\n{r.stderr[-800:]}")


def probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    return float(json.loads(r.stdout)["format"]["duration"])


def build_segment(png: Path, wav: Path, out: Path, telops: list = None) -> float:
    """telops: [(透過PNG, 開始秒, 終了秒), ...] を overlay の enable で焼き込む"""
    dur = probe_duration(wav) + PAD_SEC
    telops = telops or []

    inputs = ["-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", str(png),
              "-i", str(wav)]
    chains = []
    cur = "[0:v]"
    for i, (tpng, st, en) in enumerate(telops):
        inputs += ["-i", str(tpng)]
        nxt = f"[v{i}]"
        # テロップ帯(1920x240)を最下部に、表示時間だけ重ねる
        chains.append(f"{cur}[{i + 2}:v]overlay=0:840:enable='between(t,{st:.3f},{en:.3f})'{nxt}")
        cur = nxt
    chains.append(f"{cur}null[v]")
    chains.append(f"[1:a]apad=whole_dur={dur:.3f}[a]")

    run([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", ";".join(chains),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-t", f"{dur:.3f}",
        str(out),
    ])
    return dur


def overlay_cam(main_mp4: Path, cam: Path, out: Path) -> None:
    """右下に丸窓カムをループ重ね（円形切り抜き済み・アルファ付きcam.mov前提）"""
    run([
        "ffmpeg", "-y",
        "-i", str(main_mp4),
        "-stream_loop", "-1", "-i", str(cam),
        "-filter_complex",
        "[1:v]scale=340:-1[cam];[0:v][cam]overlay=W-w-48:H-h-48:shortest=1[v]",
        "-map", "[v]", "-map", "0:a",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(out),
    ])


def main(lesson: str) -> None:
    slide_dir = BASE / "slides" / f"lesson{lesson}"
    audio_dir = BASE / "audio" / f"lesson{lesson}"
    out_path = BASE / "out" / f"lesson{lesson}.mp4"
    (BASE / "out").mkdir(exist_ok=True)

    pngs = sorted(slide_dir.glob("seg*.png"))
    if not pngs:
        print("ERROR: スライドPNGがありません（先に make_slides.py を実行）", file=sys.stderr)
        sys.exit(1)

    # PNGとwavの対応検収
    pairs = []
    for png in pngs:
        wav = audio_dir / f"{png.stem}.wav"
        if not wav.exists():
            print(f"ERROR: 対応する音声がない: {wav.name}（先に tts.py を実行）", file=sys.stderr)
            sys.exit(1)
        pairs.append((png, wav))

    # テロップ設定と台本（ナレーション）の読み込み
    vcfg_path = BASE / "assets" / "video_config.json"
    vcfg = json.loads(vcfg_path.read_text(encoding="utf-8")) if vcfg_path.exists() else {}
    telop_on = vcfg.get("telop", True)
    narrations = {}
    seg_json = BASE / "build" / f"lesson{lesson}" / "segments.json"
    if telop_on and seg_json.exists():
        data = json.loads(seg_json.read_text(encoding="utf-8"))
        narrations = {s["id"]: s["narration"] for s in data["segments"]}
    elif telop_on:
        print("  ⚠️ segments.json がないためテロップなしで組み立てます")
        telop_on = False

    print(f"組み立て開始: {len(pairs)}セグメント（テロップ: {'あり' if telop_on else 'なし'}）")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        seg_files = []
        for png, wav in pairs:
            seg_mp4 = tmp_dir / f"{png.stem}.mp4"
            telops = []
            if telop_on and narrations.get(png.stem):
                telops = telop_mod.make_telop_pngs(
                    narrations[png.stem], probe_duration(wav), tmp_dir, png.stem
                )
            dur = build_segment(png, wav, seg_mp4, telops)
            seg_files.append(seg_mp4)
            print(f"  {png.stem}: {dur:.1f}s（テロップ{len(telops)}枚）" if telops else f"  {png.stem}: {dur:.1f}s")

        # concat（同一コーデックなのでcopy結合）
        list_file = tmp_dir / "list.txt"
        list_file.write_text("".join(f"file '{f}'\n" for f in seg_files))
        joined = tmp_dir / "joined.mp4"
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
             "-c", "copy", "-movflags", "+faststart", str(joined)])

        cam = BASE / "assets" / "cam.mov"
        if cam.exists():
            print("  cam.mov を検出 → 左下に重ねます")
            overlay_cam(joined, cam, out_path)
        else:
            joined.rename(out_path)

    total = probe_duration(out_path)
    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"完成: {out_path.relative_to(BASE)}")
    print(f"  合計 {int(total // 60)}分{total % 60:.0f}秒 / {size_mb:.1f}MB")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: assemble.py <lesson番号 例:01>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
