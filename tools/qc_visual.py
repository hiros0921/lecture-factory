#!/usr/bin/env python3
"""生成画像の自動検収。「真っ黒なまま返ってきた画像」を機械で検出する。

    python3 tools/qc_visual.py images/*.png
    python3 tools/qc_visual.py --calibrate approved/*.png      # 合格ラインを実測する
    python3 tools/qc_visual.py --min 0.46 images/*.png         # しきい値を直接指定

画像生成AIは、指示が暗い画調だと「ほぼ真っ黒で被写体が沈んだ絵」を平然と返す。
生成そのものは成功扱いなので、API のエラーでは検出できない。そして一瞬で切り替わる
カットに使うと、目視では気づけない。だから機械で測る。

しきい値の根拠（2026-08-07 に nano-banana で実測。黒基調のMV用に生成した画像）:

    指標                    承認された20枚     不良と判定した5枚
    見える面積              0.46〜39.35%       0.24 / 0.31 / 0.37 / 0.44 / 0.71%
    再生成後の同じ5枚       1.54〜31.31%       ―

重要: グレースケールで測ってはいけない。輝度は R:0.30 G:0.59 B:0.11 の重みで
合成されるため、赤が支配的な絵が不当に暗いと判定される。実際、赤信号のカットと
薔薇のカットが誤って不合格になった。RGB の最大値で測れば正しく合格する。

    指標                赤信号のカット   薔薇のカット
    グレースケール       0.05%（不合格）  0.01%（不合格）  ← 誤判定
    RGBの最大値          2.64%（合格）    5.38%（合格）    ← 正しい

しきい値は絵柄ごとに違う。明るい画調の案件でこの数値を流用してはいけない。
--calibrate に承認済みの画像を渡して、その案件の下限を測り直すこと。

較正には「似た種類のカットだけ」を渡すこと。これを守らないと判定が甘くなる。
実際に検証したところ、同じ承認済み素材でも、渡す集合を変えると下限が変わった:

    較正に使った集合                       下限      25%に暗くした画像
    サビの20枚（人物が写る同種のカット）    0.46%     検出できる
    完成MVから抽出した20枚                 0.12%     見逃す

後者には「闇に消える」演出のカットが混ざっている。意図して真っ暗にした絵を
正解に含めると、不良の真っ暗な絵と区別がつかなくなる。
演出上の暗いカットは較正から外し、必要ならそれ用のしきい値を別に持つこと。
"""
import argparse
import sys
from pathlib import Path

try:
    from PIL import Image
    import numpy as np
except ImportError:
    sys.exit("Pillow と numpy が必要です:  pip3 install Pillow numpy")

# 承認済み20枚の下限。黒基調の絵柄での実測値。
DEFAULT_MIN_VISIBLE = 0.46

# この明るさを超えた画素を「見えている」とみなす（0〜255）。
# 60 は、線画の輪郭が背景の黒から分離できる下限として実測で決めた。
VISIBLE_LEVEL = 60


def measure(path: Path) -> dict:
    """1枚の画像を測る。見える面積（%）と、参考値としての平均輝度を返す。"""
    im = Image.open(path).convert("RGB")
    a = np.asarray(im, dtype=np.uint8)
    # RGB の最大値で測る。グレースケール変換だと赤が沈む（docstring 参照）
    peak = a.max(axis=2)
    return {
        "path": path,
        "size": im.size,
        "visible": float((peak > VISIBLE_LEVEL).mean() * 100),
        "mean": float(peak.mean()),
    }


def calibrate(paths: list) -> dict:
    """承認済み画像から合格ラインを割り出す。下限をそのまま採用する。"""
    vals = [measure(p)["visible"] for p in paths]
    if not vals:
        raise ValueError("画像が1枚も渡されていません")
    return {
        "n": len(vals),
        "min": min(vals),
        "max": max(vals),
        "mean": sum(vals) / len(vals),
    }


def inspect(path: Path, min_visible: float = DEFAULT_MIN_VISIBLE) -> list:
    """不良の理由を並べて返す。空リストなら合格。"""
    m = measure(path)
    ng = []
    if m["visible"] < min_visible:
        ng.append(f"見えている面積が {m['visible']:.2f}% しかない"
                  f"（下限 {min_visible:.2f}%）。ほぼ真っ黒で被写体が沈んでいる")
    return ng


def main() -> int:
    ap = argparse.ArgumentParser(description="生成画像の自動検収")
    ap.add_argument("images", nargs="+", type=Path)
    ap.add_argument("--calibrate", action="store_true",
                    help="渡した画像を承認済みとみなし、合格ラインを実測する")
    ap.add_argument("--min", type=float, default=None,
                    help=f"合格ラインを直接指定（既定 {DEFAULT_MIN_VISIBLE}）")
    a = ap.parse_args()

    missing = [p for p in a.images if not p.exists()]
    if missing:
        for p in missing:
            print(f"  ❌ {p} が見つかりません")
        return 1

    if a.calibrate:
        r = calibrate(a.images)
        print(f"承認済み {r['n']}枚 の実測")
        print(f"  見える面積  {r['min']:.2f}% 〜 {r['max']:.2f}%   平均 {r['mean']:.2f}%")
        print(f"\n  合格ライン = {r['min']:.2f}%（下限をそのまま採用）")
        print(f"  以後   python3 tools/qc_visual.py --min {r['min']:.2f} 対象.png")
        return 0

    thr = a.min if a.min is not None else DEFAULT_MIN_VISIBLE
    print(f"{'画像':<28}{'見える面積':>11}   判定")
    print("-" * 62)
    bad = 0
    for p in a.images:
        ng = inspect(p, thr)
        m = measure(p)
        if ng:
            bad += 1
            print(f"{p.name:<28}{m['visible']:>10.2f}%   ❌ {ng[0]}")
        else:
            print(f"{p.name:<28}{m['visible']:>10.2f}%   ✅")
    print("-" * 62)
    print(f"{len(a.images)}枚中 {bad}枚が不合格（合格ライン {thr:.2f}%）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
