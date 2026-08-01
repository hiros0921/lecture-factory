#!/usr/bin/env python3
"""TTS生成（自動検収つき）。

- segments.json のナレーションを読み替え辞書で置換してからTTSに渡す
- エンジンは assets/tts_config.json で切り替え（say / minimax）
- 各wavを二段階で自動検収し、不合格なら設定を変えて最大3回リトライする
    B: qc_audio     … 波形の不良（短すぎ／長すぎ／尻切れ／音割れ／無音落ち）
    C: qc_transcribe … 読み間違い・語尾のイントネーション（whisper照合）
- 3回失敗したらファイル名と不良種別を報告して停止
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixed_clips
import qc_audio

BASE = Path(__file__).resolve().parent.parent
MAX_RETRY = 3


def load_json(path: Path, default=None):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default if default is not None else {}


def apply_yomi_dict(text: str) -> str:
    text = text.replace("**", "")  # テロップ用の強調記号はTTSでは読まない
    d = load_json(BASE / "assets" / "yomi_dict.json", {})
    d = {k: v for k, v in d.items() if not k.startswith("_")}
    # 長い語から置換（部分一致の誤爆防止）
    for k in sorted(d, key=len, reverse=True):
        text = text.replace(k, d[k])
    return text


# ---------------- エンジン ----------------

def gen_say(text: str, out_path: Path, cfg: dict, attempt: int) -> None:
    """macOS標準 say（テスト用・無料）。リトライごとに話速を微調整"""
    voice = cfg.get("voice", "Kyoko")
    rate = cfg.get("rate", 190) + (attempt - 1) * 5
    subprocess.run(
        ["say", "-v", voice, "-r", str(rate),
         "-o", str(out_path), "--data-format=LEI16@22050", text],
        check=True, capture_output=True, timeout=300,
    )


def gen_minimax(text: str, out_path: Path, cfg: dict, attempt: int) -> None:
    """fal経由MiniMax（キュー方式）。assets/.fal_key と assets/voice_id.txt が必要。

    fal公式スキーマに合わせてある（2026-07-31 に確認）:
    - output_format は "url"。既定の "hex" だと音声URLではなく16進文字列が返る
    - audio_setting.format は mp3 / pcm / flac。"wav" は無効な値
    - language_boost に "Japanese" を渡すと日本語の認識精度が上がる
    - 感情パラメータ(emotion)は使わない。量産で当たり外れが大きいため
    """
    import fal_client

    vid_path = BASE / "assets" / "voice_id.txt"
    if not vid_path.exists():
        raise RuntimeError(
            "assets/voice_id.txt がありません。\n"
            "  先にクローンを作ってください:\n"
            "    python3 tools/voice_clone.py create assets/voice/prep/reference_after.wav")
    voice_id = vid_path.read_text(encoding="utf-8").strip()

    # リトライごとに話速をわずかに変える。同じ入力を投げ直しても同じ結果になりやすいため
    speed = round(cfg.get("speed", 1.0) + (attempt - 1) * 0.03, 3)

    result = fal_client.run(cfg.get("model_id", "fal-ai/minimax/speech-02-hd"), {
        "text": text,
        "voice_setting": {"voice_id": voice_id, "speed": speed},
        "audio_setting": {
            "format": cfg.get("format", "flac"),        # flacは可逆。検収の数値が歪まない
            # ドキュメントの表記は "44100" と文字列だが、APIは整数を要求する
            "sample_rate": int(cfg.get("sample_rate", 44100)),
            "channel": 1,
        },
        "language_boost": "Japanese",
        "output_format": "url",
    })

    audio_url = (result.get("audio") or {}).get("url")
    if not audio_url:
        raise RuntimeError(f"minimaxの応答に音声URLがありません: {str(result)[:300]}")

    tmp = out_path.with_suffix(".dl")
    fal_client.download(audio_url, tmp)
    # 検収は16bit PCMで行うので、ここでwavに揃える
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(tmp), "-ac", "1", "-c:a", "pcm_s16le", str(out_path)],
        check=True, capture_output=True, timeout=120,
    )
    tmp.unlink()


ENGINES = {"say": gen_say, "minimax": gen_minimax}


# ---------------- 自動検収（B: 波形 → C: 書き起こし の二段階） ----------------

# analyze_wav は qc_audio.analyze の別名（既存の呼び出し互換のため残す）
analyze_wav = qc_audio.analyze


def inspect_all(path: Path, text: str, qc: dict, transcribe: bool) -> tuple:
    """(不良理由のリスト, whisperの聞き取り) を返す。理由が空なら合格。

    先に波形(B)を見て、通ったものだけ書き起こし(C)にかける。
    Cはwhisperを動かすぶん1本あたり数秒かかるので、明らかな不良に使うのは無駄。
    """
    ng = qc_audio.inspect(path, text, qc)
    if ng or not transcribe:
        return ng, ""

    import qc_transcribe  # whisperの読み込みが重いので必要になってから
    r = qc_transcribe.check(path, text)
    return list(r["ng"]), r["heard"]


# ---------------- メイン ----------------

def main(lesson: str, transcribe: bool = None) -> None:
    seg_path = BASE / "build" / f"lesson{lesson}" / "segments.json"
    if not seg_path.exists():
        print(f"ERROR: 先に parse_script.py を実行してください", file=sys.stderr)
        sys.exit(1)
    data = json.loads(seg_path.read_text(encoding="utf-8"))

    cfg = load_json(BASE / "assets" / "tts_config.json", {"engine": "say", "say": {}})
    engine_name = cfg.get("engine", "say")
    gen = ENGINES.get(engine_name)
    if not gen:
        print(f"ERROR: 不明なエンジン: {engine_name}", file=sys.stderr)
        sys.exit(1)

    qc = qc_audio.merge_qc(cfg, engine_name)
    if transcribe is None:
        transcribe = cfg.get("qc", {}).get("transcribe", True)

    vid_path = BASE / "assets" / "voice_id.txt"
    voice_id = vid_path.read_text().strip() if vid_path.exists() else ""

    out_dir = BASE / "audio" / f"lesson{lesson}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"TTS生成開始（エンジン: {engine_name} / 書き起こし照合: "
          f"{'あり' if transcribe else 'なし'}）", flush=True)
    failed = {}
    for seg in data["segments"]:
        wav_path = out_dir / f"{seg['id']}.wav"

        # 検証済みクリップで始まるナレーションは、その部分をTTSに読ませない
        clip, rest = fixed_clips.split_prefix(seg["narration"], engine_name, voice_id)
        tts_text = apply_yomi_dict(rest if clip else seg["narration"])

        if clip and not tts_text.strip():
            # ナレーション全体が検証済みクリップで賄える場合
            shutil.copyfile(clip, wav_path)
            a = analyze_wav(wav_path)
            print(f"  ✅ {seg['id']}.wav  {a['duration']:.1f}s"
                  f"（検証済みクリップ {clip.stem} をそのまま使用）", flush=True)
            continue

        # クリップと結合する場合、検収は「TTSで作る部分」に対して行う。
        # クリップ側は作成時に完全一致で検証済みで、しかも固有名詞なので、
        # 本編と同じ照合にかけると whisper が読めずに永久に落ち続ける。
        target = out_dir / f"{seg['id']}.part.wav" if clip else wav_path

        last_ng, last_heard = [], ""
        for attempt in range(1, MAX_RETRY + 1):
            gen(tts_text, target, cfg.get(engine_name, {}), attempt)
            last_ng, last_heard = inspect_all(target, tts_text, qc, transcribe)
            if not last_ng:
                if clip:
                    fixed_clips.join(clip, target, wav_path)
                    target.unlink()
                a = analyze_wav(wav_path)
                note = f"（試行{attempt}回目で合格"
                note += f" / 先頭に検証済みクリップ {clip.stem}）" if clip else "）"
                print(f"  ✅ {seg['id']}.wav  {a['duration']:.1f}s{note}", flush=True)
                break
            print(f"  ⚠️  {seg['id']}.wav 試行{attempt}: {' / '.join(last_ng)} → リトライ",
                  flush=True)
        else:
            failed[seg["id"]] = (last_ng, last_heard, tts_text)
            if clip and target.exists():
                # 不合格でも結合して残す。捨ててしまうと音声ファイルが無い状態になり、
                # 「聞いてみたら問題なかった」ときに組み立てへ進めなくなる。
                # 検収に落ちたこと自体は下でまとめて報告する。
                fixed_clips.join(clip, target, wav_path)
                target.unlink()

    if failed:
        sys.stdout.flush()
        print(f"\n❌ {MAX_RETRY}回リトライしても直らなかったファイル:", file=sys.stderr)
        for sid, (ng, heard, said) in failed.items():
            print(f"\n   ■ {sid}.wav", file=sys.stderr)
            for n in ng:
                print(f"     {n}", file=sys.stderr)
            if heard:
                # 読ませた文と聞き取りを並べる。ここを見れば「TTSの誤読」か
                # 「whisperの聞き間違い」かは人間なら一目で分かる。
                print(f"     読ませた文  : {said}", file=sys.stderr)
                print(f"     whisperの聞取: {heard}", file=sys.stderr)
        print("\n   対処の順番:", file=sys.stderr)
        print("   1. 上の2行を見比べる。音として正しく読めているなら、whisper側の", file=sys.stderr)
        print("      聞き間違い（特に人名・地名で起きる）なので、そのまま採用してよい。", file=sys.stderr)
        print("      その場合は --no-transcribe で通すか、検証済みクリップにする。", file=sys.stderr)
        print("   2. TTSが本当に誤読しているなら、assets/yomi_dict.json に読みを追加する。", file=sys.stderr)
        print("   3. 固有名詞で何度やっても直らないなら、", file=sys.stderr)
        print("      tools/verify_take.py で検証済みクリップを作って使い回す。", file=sys.stderr)
        print("   4. どうしても直らない文は、台本側を書き換えるのが最も速い。", file=sys.stderr)
        sys.exit(1)
    print(f"全{len(data['segments'])}ファイル合格 → {out_dir.relative_to(BASE)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="TTS生成（自動検収つき）")
    ap.add_argument("lesson", help="回番号（例: 01）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--no-transcribe", action="store_true",
                   help="書き起こし照合(C)を省く（速いが読み間違いを見逃す）")
    g.add_argument("--transcribe", action="store_true", help="書き起こし照合(C)を必ず行う")
    a = ap.parse_args()
    try:
        main(a.lesson.zfill(2),
             False if a.no_transcribe else (True if a.transcribe else None))
    except RuntimeError as e:
        # 設定の不整合など、原因と対処が分かっている失敗。
        # トレースバックを見せても何も伝わらないので、本文だけ出す。
        sys.stdout.flush()
        print(f"\n❌ {e}", file=sys.stderr)
        sys.exit(1)
