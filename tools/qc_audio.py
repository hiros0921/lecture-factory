#!/usr/bin/env python3
"""B: 波形ベースの自動検収。生成した音声の「物理的な不良」を機械で検出する。

    python3 tools/qc_audio.py audio/lesson01/seg01.wav "読ませた原文"
    python3 tools/qc_audio.py --measure audio/lesson01/*.wav   # 数値だけ一覧表示

しきい値の根拠（2026-07-31 に say/Kyoko で実測。正常27サンプル＋故意に壊した3サンプル）:

    指標          正常の範囲        尻切れ    音割れ    短すぎ
    字/秒         5.47〜9.28        7.76      7.27      69.0
    末尾ピーク    0.153〜0.287      0.235     1.000     0.253
    クリップ率    0.00000           0.00000   0.05082   0.00000
    末尾無音(秒)  0.021〜0.059      0.000     0.015     0.000

重要: 「末尾ピーク」では尻切れを判定できない。正常な疑問文は語尾が上がるため
末尾ピークが 0.287 まで上がり、尻切れ(0.235)より高くなる。原理的に分離できない。
代わりに「末尾に無音があるか」で判定する。音の途中でファイルが終わっていれば尻切れ。

MiniMax に切り替えたら、--measure で分布を取り直して tts_config.json の
エンジン別 qc を調整すること。エンジンが変われば波形の性質も変わる。
"""
import argparse
import json
import math
import subprocess
import sys
import tempfile
import wave
from array import array
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

# 既定のしきい値。tts_config.json の "qc"（全体）とエンジン別 "qc" で上書きできる。
DEFAULT_QC = {
    "chars_per_sec": 7.2,       # 実測の平均（say/Kyoko）。想定秒数 = 文字数 / この値
    "min_duration_ratio": 0.55,  # 想定秒数のこの割合を下回れば「短すぎ」
    "max_duration_ratio": 2.0,   # 想定秒数のこの倍を超えれば「長すぎ」（無音・ノイズ混入）
    "tail_silence_min": 0.010,   # 末尾にこの秒数の無音が無ければ「尻切れ」
    "silence_floor": 0.010,      # 無音とみなす振幅（フルスケール比。約 -40dBFS）
    "clip_ratio_max": 0.001,     # フルスケール張り付きがこの比率を超えれば「音割れ」
    "peak_max": 0.999,           # ピークがこの値以上なら「音割れ」
    "rms_db_min": -40.0,         # 全体RMSがこれを下回れば「無音落ち」
}


def merge_qc(cfg: dict, engine: str) -> dict:
    """既定 → 全体設定 → エンジン別設定 の順に上書きしたしきい値を返す。"""
    qc = dict(DEFAULT_QC)
    qc.update(cfg.get("qc", {}))
    qc.update(cfg.get(engine, {}).get("qc", {}))
    return qc


# ---------------- 波形の計測 ----------------

def _read_16bit(path: Path):
    """16bit モノラルのサンプル列と サンプリングレートを返す。

    16bit 以外（MiniMax が 24bit や 32bit float で返す場合）は ffmpeg で変換してから読む。
    ここを素通りさせると、検収が何も検査せずに合格を返してしまう。
    """
    try:
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() == 2:
                n, rate, ch = w.getnframes(), w.getframerate(), w.getnchannels()
                a = array("h")
                a.frombytes(w.readframes(n))
                return (a[::ch] if ch > 1 else a), rate
    except wave.Error:
        pass  # wav ではない、または圧縮形式 → 下で変換する

    with tempfile.TemporaryDirectory() as tmp:
        conv = Path(tmp) / "conv.wav"
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-c:a", "pcm_s16le", "-ac", "1", str(conv)],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0 or not conv.exists():
            raise RuntimeError(f"音声を16bitに変換できません: {path.name}\n{r.stderr[-300:]}")
        with wave.open(str(conv), "rb") as w:
            n, rate = w.getnframes(), w.getframerate()
            a = array("h")
            a.frombytes(w.readframes(n))
            return a, rate


def analyze(path: Path) -> dict:
    """波形の統計を取る。すべて 0.0〜1.0 のフルスケール比（rms_db のみ dBFS）。"""
    samples, rate = _read_16bit(path)
    total = len(samples)
    if total == 0 or rate == 0:
        return {"duration": 0.0, "peak": 0.0, "tail_peak": 0.0, "clip_ratio": 0.0,
                "rms_db": -99.0, "tail_silence": 0.0}

    full = 32767.0
    peak = max(max(samples), -min(samples)) / full
    clip_level = 32766
    clip_ratio = sum(1 for s in samples if s >= clip_level or s <= -clip_level) / total
    rms = math.sqrt(sum(float(s) * s for s in samples) / total) / full
    rms_db = 20 * math.log10(rms) if rms > 0 else -99.0

    tail_n = max(1, int(rate * 0.25))
    tail_peak = max(max(samples[-tail_n:]), -min(samples[-tail_n:])) / full

    # 末尾の無音長: 後ろから見て、無音の床を超える音が出るまでの長さ
    floor = int(full * DEFAULT_QC["silence_floor"])
    q = 0
    for s in reversed(samples):
        if s > floor or s < -floor:
            break
        q += 1

    return {"duration": total / rate, "peak": peak, "tail_peak": tail_peak,
            "clip_ratio": clip_ratio, "rms_db": rms_db, "tail_silence": q / rate}


# ---------------- 判定 ----------------

def inspect(path: Path, text: str, qc: dict = None) -> list:
    """不良理由のリストを返す（空なら合格）。text は実際に読ませたテキスト。"""
    qc = qc or DEFAULT_QC
    a = analyze(path)
    ng = []

    expected = len(text) / qc["chars_per_sec"] if text else 0.0
    if expected:
        if a["duration"] < expected * qc["min_duration_ratio"]:
            ng.append(f"短すぎ（{a['duration']:.2f}s / 想定{expected:.2f}s）")
        elif a["duration"] > expected * qc["max_duration_ratio"]:
            ng.append(f"長すぎ（{a['duration']:.2f}s / 想定{expected:.2f}s・無音やノイズの混入疑い）")

    if a["tail_silence"] < qc["tail_silence_min"]:
        ng.append(f"尻切れ（末尾の無音{a['tail_silence']*1000:.0f}ms "
                  f"< {qc['tail_silence_min']*1000:.0f}ms・音の途中で終わっている）")

    if a["clip_ratio"] > qc["clip_ratio_max"] or a["peak"] >= qc["peak_max"]:
        ng.append(f"音割れ（クリップ率{a['clip_ratio']:.5f} / ピーク{a['peak']:.3f}）")

    if a["rms_db"] < qc["rms_db_min"]:
        ng.append(f"無音落ち（RMS {a['rms_db']:.1f}dB < {qc['rms_db_min']}dB）")

    return ng


def _measure(paths: list) -> None:
    print(f"{'ファイル':<28}{'秒':>7}{'ピーク':>8}{'末尾ピーク':>10}"
          f"{'クリップ率':>11}{'RMS dB':>9}{'末尾無音ms':>11}")
    print("-" * 84)
    for p in paths:
        a = analyze(Path(p))
        print(f"{Path(p).name:<28}{a['duration']:>7.2f}{a['peak']:>8.3f}{a['tail_peak']:>10.3f}"
              f"{a['clip_ratio']:>11.5f}{a['rms_db']:>9.1f}{a['tail_silence']*1000:>11.0f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="音声の自動検収")
    ap.add_argument("wav", nargs="+", help="検査する wav")
    ap.add_argument("text", nargs="?", default="", help="読ませた原文（1ファイル指定時のみ）")
    ap.add_argument("--measure", action="store_true", help="判定せず数値だけ一覧表示")
    a = ap.parse_args()

    if a.measure:
        _measure(a.wav)
        sys.exit(0)

    cfg = json.loads((BASE / "assets" / "tts_config.json").read_text(encoding="utf-8"))
    qc = merge_qc(cfg, cfg.get("engine", "say"))
    bad = 0
    for w in a.wav:
        ng = inspect(Path(w), a.text, qc)
        if ng:
            bad += 1
            print(f"❌ {Path(w).name}: {' / '.join(ng)}")
        else:
            print(f"✅ {Path(w).name}")
    sys.exit(1 if bad else 0)
