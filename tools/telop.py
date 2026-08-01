#!/usr/bin/env python3
"""フルテロップ生成: ナレーションを文単位に分割し、音声の実測時間に文字数比で割り付ける。

各文をHTML+CSSで透過PNGに描画（スライドとデザイン統一）し、
assemble.py が overlay の enable=between(t,開始,終了) で焼き込む。
- 台本の `**強調**` は金色で表示（TTSでは読まれない）
- 強調は1色のみ・1本2〜3箇所まで推奨
"""
import html
import re
import subprocess
from pathlib import Path

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
STRIP_W, STRIP_H = 1920, 240   # テロップ帯のサイズ（動画最下部に重ねる）

TELOP_HTML = """<!DOCTYPE html>
<html lang="ja">
<head><meta charset="UTF-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { width: 1920px; height: 240px; overflow: hidden; background: transparent; }
  .strip {
    width: 1920px; height: 240px;
    display: flex; align-items: flex-end; justify-content: center;
    padding-bottom: 64px;
  }
  .telop {
    max-width: 1000px;
    font-family: "Hiragino Sans", sans-serif;
    font-size: 44px; font-weight: 600; line-height: 1.55;
    color: #ffffff; text-align: center;
    letter-spacing: 0.03em;
    text-shadow:
      -2px -2px 0 #10151f, 2px -2px 0 #10151f,
      -2px 2px 0 #10151f, 2px 2px 0 #10151f,
      0 0 14px rgba(16,21,31,0.9);
  }
  .em { color: #d9bc80; }
</style></head>
<body><div class="strip"><div class="telop">__TEXT__</div></div></body>
</html>
"""


def split_sentences(text: str) -> list:
    """文単位に分割（。！？で区切り、区切り文字は前の文に含める）"""
    text = re.sub(r"\s+", "", text)
    parts = re.split(r"(?<=[。！？!?])", text)
    return [p for p in parts if p]


def to_html_text(sentence: str) -> str:
    """エスケープしてから **強調** を金色spanに変換"""
    escaped = html.escape(sentence)
    return re.sub(r"\*\*(.+?)\*\*", r'<span class="em">\1</span>', escaped)


def make_telop_pngs(narration: str, audio_dur: float, out_dir: Path, seg_id: str) -> list:
    """文ごとの透過PNGを生成し、[(pngパス, 開始秒, 終了秒), ...] を返す"""
    sentences = split_sentences(narration)
    if not sentences:
        return []

    plain = [re.sub(r"\*\*", "", s) for s in sentences]
    total = sum(len(p) for p in plain) or 1

    result = []
    t = 0.0
    for i, (sent, pl) in enumerate(zip(sentences, plain), 1):
        dur = audio_dur * len(pl) / total
        html_path = out_dir / f"{seg_id}_t{i:02d}.html"
        png_path = out_dir / f"{seg_id}_t{i:02d}.png"
        html_path.write_text(
            TELOP_HTML.replace("__TEXT__", to_html_text(sent)), encoding="utf-8"
        )
        r = subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
             "--force-device-scale-factor=1",
             "--default-background-color=00000000",
             f"--window-size={STRIP_W},{STRIP_H}",
             f"--screenshot={png_path}",
             f"file://{html_path}"],
            capture_output=True, text=True, timeout=60,
        )
        if not png_path.exists():
            raise RuntimeError(f"テロップ描画失敗: {seg_id} 文{i}\n{r.stderr[-300:]}")
        result.append((png_path, t, t + dur))
        t += dur
    return result
