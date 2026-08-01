#!/usr/bin/env python3
"""D: 固有名詞の根治ツール。「一度だけ完璧に読めたテイク」を作って使い回す。

同じ文を繰り返しTTS生成 → whisperで書き起こし → 原文と一致したテイクだけ合格
→ assets/voice/verified/<名前>.wav に保存し、index.json に登録する。
以後、その文で始まるナレーションは tts.py がクリップに差し替える。

    python3 tools/verify_take.py "みなさん、こんにちは。諏訪です。" greeting

判定は完全一致（正規化後）。固有名詞のためのしくみなので、
本編の照合(qc_transcribe)のような類似度の許容はしない。
波形の検収(qc_audio)も通す。使い回すクリップに尻切れがあっては困る。
"""
import argparse
import datetime
import shutil
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixed_clips
import qc_audio
import qc_transcribe

MAX_TAKES = 5


def main(text: str, name: str, max_takes: int) -> None:
    from tts import ENGINES, apply_yomi_dict, load_json

    cfg = load_json(BASE / "assets" / "tts_config.json", {"engine": "say", "say": {}})
    engine_name = cfg.get("engine", "say")
    gen = ENGINES[engine_name]
    qc = qc_audio.merge_qc(cfg, engine_name)

    vid_path = BASE / "assets" / "voice_id.txt"
    voice_id = vid_path.read_text().strip() if vid_path.exists() else ""

    tts_text = apply_yomi_dict(text)
    target = qc_transcribe.normalize(text)

    out_path = fixed_clips.VERIFIED_DIR / f"{name}.wav"
    fixed_clips.VERIFIED_DIR.mkdir(parents=True, exist_ok=True)

    print(f"検証テイク生成（最大{max_takes}回 / エンジン: {engine_name}）")
    print(f"  原文: {text}")
    if tts_text != text:
        print(f"  TTSに渡す: {tts_text}")

    with tempfile.TemporaryDirectory() as tmp:
        for take in range(1, max_takes + 1):
            wav = Path(tmp) / f"take{take}.wav"
            gen(tts_text, wav, cfg.get(engine_name, {}), take)

            ng = qc_audio.inspect(wav, tts_text, qc)
            if ng:
                print(f"  ⚠️ テイク{take}: 波形が不良（{' / '.join(ng)}）")
                continue

            heard = qc_transcribe.transcribe(wav)
            if qc_transcribe.normalize(heard) == target:
                # tempfile は別ファイルシステムのことがあるので rename ではなく move
                shutil.move(str(wav), str(out_path))
                fixed_clips.register(
                    name, text, out_path, engine_name, voice_id, take,
                    datetime.date.today().isoformat(),
                )
                print(f"  ✅ テイク{take}: 完全一致 → {out_path.relative_to(BASE)}")
                print(f"     index.json に登録しました（engine={engine_name}"
                      f" voice_id={voice_id or '(なし)'}）")
                return
            print(f"  ⚠️ テイク{take}: 不一致")
            print(f"      聞こえた: {heard}")

    print(f"\n❌ {max_takes}回とも一致しませんでした。", file=sys.stderr)
    print("   対処:", file=sys.stderr)
    print("   1. 上の「聞こえた」がwhisperの表記ゆれだけなら、音は正しく出ています。", file=sys.stderr)
    print("      その場合は読ませる文を少し変える（読点を足す等）と通ることがあります。", file=sys.stderr)
    print("   2. TTSが本当に誤読しているなら assets/yomi_dict.json に読みを追加してください。", file=sys.stderr)
    print("   3. それでも直らないなら、その語を使わない言い回しに変えてください。", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="検証済みクリップを作る")
    ap.add_argument("text", help="読ませる文（この文で始まるナレーションに使われます）")
    ap.add_argument("name", help="保存名（例: greeting）")
    ap.add_argument("--takes", type=int, default=MAX_TAKES, help=f"最大テイク数（既定{MAX_TAKES}）")
    a = ap.parse_args()
    main(a.text, a.name, a.takes)
