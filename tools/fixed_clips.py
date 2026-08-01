#!/usr/bin/env python3
"""D: 検証済みクリップの管理と結合。

固有名詞、特に人名は、読み替え辞書をどう書いても安定しないことがある。
そこで「毎回読ませる」のをやめ、「一度だけ完璧に読めたテイクを使い回す」。

    python3 tools/fixed_clips.py --list          # 登録済みクリップの一覧
    python3 tools/fixed_clips.py --check         # エンジンとの整合を確認

クリップの作成は tools/verify_take.py が行う。このモジュールは
tts.py から呼ばれ、ナレーション先頭が登録済みの文と一致したら、
その部分をTTSに読ませずクリップに差し替える。
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
VERIFIED_DIR = BASE / "assets" / "voice" / "verified"
INDEX = VERIFIED_DIR / "index.json"
GAP_SEC = 0.25   # クリップと本編の間に入れる「間」


def load_index() -> dict:
    if not INDEX.exists():
        return {}
    return json.loads(INDEX.read_text(encoding="utf-8"))


def save_index(idx: dict) -> None:
    VERIFIED_DIR.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def register(name: str, text: str, clip: Path, engine: str, voice_id: str,
             takes: int, created: str) -> None:
    idx = load_index()
    idx[name] = {
        "text": text,
        "clip": str(clip.relative_to(BASE)),
        "engine": engine,
        "voice_id": voice_id,
        "takes": takes,
        "created": created,
    }
    save_index(idx)


def split_prefix(narration: str, engine: str, voice_id: str = "") -> tuple:
    """ナレーション先頭が検証済みの文と一致すれば (クリップのPath, 残りの文) を返す。

    一致しなければ (None, narration) を返す。
    長い文から順に見る（「こんにちは。諏訪です。」と「こんにちは。」の両方が
    登録されている場合、長い方を優先する）。
    """
    idx = load_index()
    text = narration.replace("**", "").lstrip()
    for name, e in sorted(idx.items(), key=lambda kv: len(kv[1]["text"]), reverse=True):
        if not text.startswith(e["text"]):
            continue
        clip = BASE / e["clip"]
        if not clip.exists():
            raise RuntimeError(
                f"検証済みクリップ「{name}」のファイルがありません: {e['clip']}\n"
                f"  tools/verify_take.py で作り直してください。")
        if e["engine"] != engine or (e.get("voice_id", "") or "") != (voice_id or ""):
            # 声が違うクリップを繋ぐと、そこだけ別人が喋る。黙って続けてはいけない。
            raise RuntimeError(
                f"検証済みクリップ「{name}」は別の声で作られています。\n"
                f"  クリップ: engine={e['engine']} voice_id={e.get('voice_id') or '(なし)'}\n"
                f"  現在の設定: engine={engine} voice_id={voice_id or '(なし)'}\n"
                f"  声が混ざるので、次のコマンドで作り直してください:\n"
                f'    python3 tools/verify_take.py "{e["text"]}" {name}')
        return clip, text[len(e["text"]):].lstrip()
    return None, narration


def _probe_rate(path: Path) -> int:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    return int(r.stdout.strip() or 22050)


def join(clip: Path, body: Path, out: Path, gap: float = GAP_SEC) -> None:
    """検証済みクリップ + 無音の「間」 + 本編 を1つのwavに結合する。"""
    rate = _probe_rate(body)
    fmt = f"aformat=sample_fmts=s16:sample_rates={rate}:channel_layouts=mono"
    r = subprocess.run(
        ["ffmpeg", "-y",
         "-i", str(clip),
         "-f", "lavfi", "-t", f"{gap:.3f}", "-i", f"anullsrc=r={rate}:cl=mono",
         "-i", str(body),
         "-filter_complex",
         f"[0:a]{fmt}[a0];[1:a]{fmt}[a1];[2:a]{fmt}[a2];[a0][a1][a2]concat=n=3:v=0:a=1[a]",
         "-map", "[a]", "-c:a", "pcm_s16le", str(out)],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"クリップの結合に失敗しました\n{r.stderr[-500:]}")


def _list() -> None:
    idx = load_index()
    if not idx:
        print("検証済みクリップはまだありません。")
        print('作成: python3 tools/verify_take.py "みなさん、こんにちは。諏訪です。" greeting')
        return
    print(f"{'名前':<14}{'エンジン':<10}{'作成日':<12}{'テイク':>6}  原文")
    print("-" * 78)
    for name, e in idx.items():
        mark = "" if (BASE / e["clip"]).exists() else "  ⚠️ファイルなし"
        print(f"{name:<14}{e['engine']:<10}{e.get('created', '-'):<12}"
              f"{e.get('takes', '-'):>6}  {e['text']}{mark}")


def _check() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tts import load_json

    cfg = load_json(BASE / "assets" / "tts_config.json", {"engine": "say"})
    engine = cfg.get("engine", "say")
    vid_path = BASE / "assets" / "voice_id.txt"
    voice_id = vid_path.read_text().strip() if vid_path.exists() else ""

    idx = load_index()
    if not idx:
        print("検証済みクリップはまだありません。")
        return
    bad = 0
    print(f"現在の設定: engine={engine} voice_id={voice_id or '(なし)'}\n")
    for name, e in idx.items():
        problems = []
        if not (BASE / e["clip"]).exists():
            problems.append("ファイルがない")
        if e["engine"] != engine:
            problems.append(f"エンジンが違う（{e['engine']}）")
        if (e.get("voice_id", "") or "") != (voice_id or ""):
            problems.append("voice_idが違う")
        if problems:
            bad += 1
            print(f"  ❌ {name}: {' / '.join(problems)}")
        else:
            print(f"  ✅ {name}")
    if bad:
        sys.stdout.flush()
        print(f"\n{bad}件が現在の声と合いません。該当のクリップを作り直してください。",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="検証済みクリップの管理")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="登録済みクリップの一覧")
    g.add_argument("--check", action="store_true", help="現在のエンジン・声との整合を確認")
    a = ap.parse_args()
    _list() if a.list else _check()
