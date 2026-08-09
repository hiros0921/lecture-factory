#!/usr/bin/env python3
"""生成動画の自動検収。「動いていないまま返ってきたクリップ」を機械で検出する。

    python3 tools/qc_motion.py clips/*.mp4
    python3 tools/qc_motion.py --calibrate approved/*.mp4    # 合格ラインを実測する
    python3 tools/qc_motion.py --min 22.6 clips/*.mp4        # しきい値を直接指定

動画生成AIは、動きの指示が控えめだと「ほぼ静止した5秒」を平然と返す。
生成は成功扱いなので API のエラーでは検出できない。そして 0.3秒で切り替わる
カットに使うと、目視ではまず気づけない。だから機械で測る。

しきい値の根拠（2026-08-07 に Kling v2.5 turbo pro で実測。黒基調のMV用に生成）:

    指標              承認された8本    静止して返ってきた7本
    masked差分        22.6〜110.5      0.03〜0.59

    生成10本のうち7本が静止だった。原因は生成側ではなくこちらの指示で、
    「動きは控えめに」と書いたためモデルが忠実に何も動かさない映像を返した。

重要: 単純な平均差分では、疎な線画の判定を誤る。黒が9割の画面で細い線が
平行移動しても、変化する画素がごくわずかしかないため差分が小さく出る。
明るい画素だけを見る（masked）方式にすると、承認済みと不良が分離できる。

    クリップ        平均差分   masked差分   実際
    密度のある絵     1.69       6.06        カメラワークあり
    疎な線画         0.91       6.62        カメラワークあり（平均差分では不合格）
    静止クリップ     0.04       ―           本当に動いていない

さらに重要: masked でも、コヒーレントな平行移動は過小評価される。
実際、この指標で不合格とした7本のうち4本は、目で見ると十分動いていた。
指標を信じ切っていれば不要な作り直しに $1.40 使っていた。

再現できる。静止画に一定のズームを付けたクリップを測るとこうなる:

    完全静止            0.00
    4秒で 2% ズーム     3.38
    4秒で 8% ズーム     5.44
    4秒で20% ズーム    11.96   ← 目には明確に動いて見えるが、下限16.70を下回る

そこで二者択一をやめ、三分岐にした。

    合格      下限以上          そのまま使う
    要確認    STILL 〜 下限     カメラワークかもしれない。コマを並べて目で見る
    不良      STILL 未満        本当に動いていない。作り直す

STILL(=1.0) の根拠は上の実測。完全静止は 0.00 に張り付き、最も緩い動き（2%ズーム）
でも 3.38 出る。その間に線を引けば、静止と「動いているが指標が低い」を分けられる。
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image
    import numpy as np
except ImportError:
    sys.exit("Pillow と numpy が必要です:  pip3 install Pillow numpy")

# 承認済み8本の下限。黒基調・カメラワークありの素材での実測値。
DEFAULT_MIN_MOTION = 22.6

SAMPLE_FPS = 4      # 1秒あたり何コマ見るか
SAMPLE_MAX = 16     # 何コマまで見るか（4秒ぶん）
THUMB = (160, 90)   # 縮小してから比較する。ノイズの影響を避けるため
LIT_LEVEL = 30      # この明るさを超えた画素だけを比較対象にする（docstring 参照）
STILL = 1.0         # これ未満は「完全静止」。実測で 0.00 に張り付く（docstring 参照）


def _frames(path: Path) -> list:
    """動画から等間隔でコマを抜き、グレースケールの配列にして返す。"""
    tmp = Path(tempfile.mkdtemp(prefix="qcmotion_"))
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(path),
             "-vf", f"fps={SAMPLE_FPS},scale={THUMB[0]}:{THUMB[1]}",
             "-frames:v", str(SAMPLE_MAX), str(tmp / "%03d.png")],
            capture_output=True, text=True, timeout=300)
        files = sorted(tmp.glob("*.png"))
        if not files:
            raise RuntimeError(f"コマを抽出できません\n{r.stderr[-300:]}")
        return [np.asarray(Image.open(f).convert("L"), dtype=float) for f in files]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def measure(path: Path) -> dict:
    """1本のクリップを測る。masked差分（主指標）と平均差分（参考）を返す。"""
    a = _frames(path)
    if len(a) < 2:
        return {"path": path, "frames": len(a), "masked": 0.0, "plain": 0.0}
    masked, plain = [], []
    for i in range(len(a) - 1):
        d = np.abs(a[i + 1] - a[i])
        plain.append(float(d.mean()))
        # 明るい画素だけを見る。黒の海で平均を取ると疎な絵が沈む（docstring 参照）
        m = np.maximum(a[i], a[i + 1]) > LIT_LEVEL
        if m.sum() > 20:
            masked.append(float(d[m].mean()))
    return {
        "path": path,
        "frames": len(a),
        "masked": float(np.mean(masked)) if masked else 0.0,
        "plain": float(np.mean(plain)) if plain else 0.0,
    }


def calibrate(paths: list) -> dict:
    """承認済みクリップから合格ラインを割り出す。下限をそのまま採用する。"""
    vals = [measure(p)["masked"] for p in paths]
    if not vals:
        raise ValueError("動画が1本も渡されていません")
    return {"n": len(vals), "min": min(vals), "max": max(vals),
            "mean": sum(vals) / len(vals)}


def judge(path: Path, min_motion: float = DEFAULT_MIN_MOTION) -> tuple:
    """(区分, 理由, 実測値) を返す。区分は ok / check / ng の3種。"""
    m = measure(path)
    if m["frames"] < 2:
        return "ng", "コマが1枚しか取れない。動画として壊れている", m
    if m["masked"] < STILL:
        return "ng", (f"動き量が {m['masked']:.2f} しかない。"
                      f"完全に静止したまま返ってきている"), m
    if m["masked"] < min_motion:
        return "check", (f"動き量 {m['masked']:.2f}（下限 {min_motion:.2f}）。"
                         f"カメラワークの可能性がある。コマを並べて目で見ること"), m
    return "ok", "", m


def inspect(path: Path, min_motion: float = DEFAULT_MIN_MOTION) -> list:
    """不良の理由を並べて返す。空リストなら合格。要確認は不良に含めない。"""
    kind, why, _ = judge(path, min_motion)
    return [why] if kind == "ng" else []


def main() -> int:
    ap = argparse.ArgumentParser(description="生成動画の自動検収")
    ap.add_argument("clips", nargs="+", type=Path)
    ap.add_argument("--calibrate", action="store_true",
                    help="渡した動画を承認済みとみなし、合格ラインを実測する")
    ap.add_argument("--min", type=float, default=None,
                    help=f"合格ラインを直接指定（既定 {DEFAULT_MIN_MOTION}）")
    a = ap.parse_args()

    missing = [p for p in a.clips if not p.exists()]
    if missing:
        for p in missing:
            print(f"  ❌ {p} が見つかりません")
        return 1

    if a.calibrate:
        r = calibrate(a.clips)
        print(f"承認済み {r['n']}本 の実測")
        print(f"  動き量  {r['min']:.2f} 〜 {r['max']:.2f}   平均 {r['mean']:.2f}")
        print(f"\n  合格ライン = {r['min']:.2f}（下限をそのまま採用）")
        print(f"  以後   python3 tools/qc_motion.py --min {r['min']:.2f} 対象.mp4")
        return 0

    thr = a.min if a.min is not None else DEFAULT_MIN_MOTION
    print(f"{'クリップ':<24}{'動き量':>9}{'(参考)平均':>11}   判定")
    print("-" * 66)
    ng, check = [], []
    for p in a.clips:
        kind, why, m = judge(p, thr)
        mark = {"ok": "✅ 合格", "check": "⚠️  要確認", "ng": "❌ 不良"}[kind]
        if kind == "ng":
            ng.append(p)
        elif kind == "check":
            check.append(p)
        print(f"{p.name:<24}{m['masked']:>9.2f}{m['plain']:>11.2f}   {mark}"
              + (f"  {why}" if why else ""))
    print("-" * 66)
    print(f"{len(a.clips)}本中  合格 {len(a.clips)-len(ng)-len(check)} / "
          f"要確認 {len(check)} / 不良 {len(ng)}"
          f"（合格ライン {thr:.2f}、静止判定 {STILL:.2f}）")
    if check:
        print("\n  要確認のものは、作り直す前にコマを並べて目視すること。")
        print("  この指標はコヒーレントな平行移動を過小評価する（docstring 参照）。")
    return 1 if ng else 0


if __name__ == "__main__":
    sys.exit(main())
