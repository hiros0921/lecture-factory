#!/usr/bin/env python3
"""A: ボイスクローンの参照音声づくりとクローン管理。

    # 参照音声の前処理（複数ファイルを繋いで1本にできる）
    python3 tools/voice_clone.py prep "assets/voice/新規録音 73.m4a" "assets/voice/新規録音 74.m4a"

    # クローン作成（課金が発生する。確認を求められる）
    python3 tools/voice_clone.py create assets/voice/prep/reference_after.wav

処理前と処理後の両方を assets/voice/prep/ に残すので、聴き比べられる。

前処理でやること:
  1. 48kHz モノラルの wav に統一
  2. ハイパスで低域のゴロつき（机の振動・エアコンの唸り）を落とす
  3. ラウドネス正規化（2パス）で音量を揃える
  4. 前後の無音を詰める
  5. 複数ファイルなら 0.4秒の間を空けて結合

ノイズ除去は既定で行わない。iPhoneのボイスメモは録音時点でノイズゲートが
掛かっており、語間が完全な無音になっている。そこへ更にノイズ除去を掛けると、
消すものが無いまま声の子音まで削れて、かえって不自然になる。
明らかな暗騒音がある録音にだけ --denoise を付ける。

クローンについての注意（fal公式仕様）:
  作ったクローンは「7日以内に一度TTSで使わないと自動削除される」。
  そのため create は作成直後に必ずテスト生成まで行う。
"""
import argparse
import base64
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fal_client
import qc_audio

CLONE_MODEL = "fal-ai/minimax/voice-clone"
TTS_MODEL = "fal-ai/minimax/speech-02-hd"
# クローン直後の確認用。挨拶・固有名詞・疑問形を1文ずつ含めてある。
# この3つはいずれも本編で必ず使い、かつ崩れやすい要素。
TEST_TEXT = ("みなさん、こんにちは。株式会社ライテックの諏訪です。"
             "今日は、動画講義の作り方についてお話しします。準備はいいですか？")

PREP_DIR = BASE / "assets" / "voice" / "prep"
RATE = 48000
GAP_SEC = 0.4          # 結合するときの間
TARGET_I = -18.0       # ラウドネス目標（音声素材の標準的な値）
TARGET_TP = -1.5       # トゥルーピーク上限。0に近づけるとクリップの危険がある
TARGET_LRA = 11.0
HIGHPASS_HZ = 70       # これ以下に人の声の成分はほぼ無い


def run(cmd: list, timeout: int = 600) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"コマンド失敗: {' '.join(str(c) for c in cmd[:5])}...\n{r.stderr[-600:]}")
    return r.stderr


def to_wav(src: Path, dst: Path) -> None:
    """無加工で 48kHz モノラル wav にするだけ（聴き比べの「処理前」）。"""
    run(["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", str(RATE),
         "-c:a", "pcm_s16le", str(dst)])


def measure_loudness(src: Path, filters: str) -> dict:
    """loudnorm の1パス目。実測値を取る。"""
    err = run(["ffmpeg", "-i", str(src), "-af",
               f"{filters},loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}:print_format=json",
               "-f", "null", "-"])
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.S)
    if not m:
        raise RuntimeError("loudnorm の測定結果を読み取れませんでした")
    return json.loads(m.group(0))


def prep_one(src: Path, dst: Path, denoise: bool) -> dict:
    """1ファイルを整える。返り値は測定値（報告用）。"""
    chain = [f"highpass=f={HIGHPASS_HZ}"]
    if denoise:
        chain.append("afftdn=nr=10:nf=-30")
    filters = ",".join(chain)

    stats = measure_loudness(src, filters)
    # 2パス目。1パス目の実測値を渡すと、区間ごとに音量が揺れずに揃う
    norm = (f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}"
            f":measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
            f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
            f":offset={stats['target_offset']}:linear=true:print_format=summary")
    # 前後の無音だけ詰める。途中の「間」は話し方そのものなので残す
    trim = ("silenceremove=start_periods=1:start_silence=0.15:start_threshold=-50dB:detection=peak,"
            "areverse,"
            "silenceremove=start_periods=1:start_silence=0.15:start_threshold=-50dB:detection=peak,"
            "areverse")
    run(["ffmpeg", "-y", "-i", str(src), "-af", f"{filters},{norm},{trim}",
         "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", str(dst)])
    return stats


def concat(parts: list, dst: Path, gap: float = GAP_SEC) -> None:
    """複数の wav を 0.4秒の間を空けて繋ぐ。"""
    if len(parts) == 1:
        run(["ffmpeg", "-y", "-i", str(parts[0]), "-c:a", "pcm_s16le", str(dst)])
        return
    inputs, chain, labels = [], [], []
    for i, p in enumerate(parts):
        inputs += ["-i", str(p)]
        chain.append(f"[{i}:a]aformat=sample_fmts=s16:sample_rates={RATE}:channel_layouts=mono[p{i}]")
        labels.append(f"[p{i}]")
    sil_idx = len(parts)
    inputs += ["-f", "lavfi", "-t", f"{gap:.3f}", "-i", f"anullsrc=r={RATE}:cl=mono"]
    chain.append(f"[{sil_idx}:a]aformat=sample_fmts=s16:sample_rates={RATE}:channel_layouts=mono[sil]")

    seq, n = [], 0
    for i, lab in enumerate(labels):
        if i:
            seq.append("[sil]")
            n += 1
        seq.append(lab)
        n += 1
    chain.append("".join(seq) + f"concat=n={n}:v=0:a=1[out]")
    run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(chain),
         "-map", "[out]", "-c:a", "pcm_s16le", str(dst)])


def cmd_prep(sources: list, name: str, denoise: bool) -> None:
    PREP_DIR.mkdir(parents=True, exist_ok=True)
    srcs = [Path(s) for s in sources]
    for s in srcs:
        if not s.exists():
            print(f"ERROR: 音声が見つかりません: {s}", file=sys.stderr)
            sys.exit(1)

    print(f"参照音声の前処理（{len(srcs)}ファイル / ノイズ除去: {'あり' if denoise else 'なし'}）\n")

    before_parts, after_parts = [], []
    for i, s in enumerate(srcs, 1):
        stem = f"{name}_{i:02d}"
        before = PREP_DIR / f"{stem}_before.wav"
        after = PREP_DIR / f"{stem}_after.wav"
        to_wav(s, before)
        stats = prep_one(s, after, denoise)
        before_parts.append(before)
        after_parts.append(after)

        b, a = qc_audio.analyze(before), qc_audio.analyze(after)
        print(f"  [{i}] {s.name}")
        print(f"      処理前: {b['duration']:6.2f}s  ピーク {b['peak']:.3f}  RMS {b['rms_db']:6.1f}dB"
              f"  （実測ラウドネス {float(stats['input_i']):.1f} LUFS）")
        print(f"      処理後: {a['duration']:6.2f}s  ピーク {a['peak']:.3f}  RMS {a['rms_db']:6.1f}dB")

    out_before = PREP_DIR / f"{name}_before.wav"
    out_after = PREP_DIR / f"{name}_after.wav"
    concat(before_parts, out_before)
    concat(after_parts, out_after)

    fb, fa = qc_audio.analyze(out_before), qc_audio.analyze(out_after)
    print(f"\n  完成: {out_after.relative_to(BASE)}")
    print(f"      長さ {fa['duration']:.2f}s / ピーク {fa['peak']:.3f} / RMS {fa['rms_db']:.1f}dB")
    print(f"  聴き比べ用（処理前）: {out_before.relative_to(BASE)}  長さ {fb['duration']:.2f}s")

    ng = []
    if fa["peak"] >= 0.999:
        ng.append("音割れしています")
    if not (10.0 <= fa["duration"] <= 300.0):
        ng.append(f"長さが {fa['duration']:.0f}秒です（10〜300秒に収めてください）")
    if ng:
        print("\n  ⚠️  " + " / ".join(ng), file=sys.stderr)
        sys.exit(1)
    print("\n  検収OK: 音割れなし・長さも適正です")


def to_data_uri(src: Path) -> str:
    """音声を mp3 にしてから base64 の data URI にする。

    wav のまま送ると 121秒で15MB超になり、リクエストが重くなる。
    元の録音が 64kbps の圧縮なので、192kbps の mp3 で品質はほぼ落ちない。
    """
    tmp = src.with_suffix(".upload.mp3")
    run(["ffmpeg", "-y", "-i", str(src), "-c:a", "libmp3lame", "-b:a", "192k",
         "-ac", "1", str(tmp)])
    b64 = base64.b64encode(tmp.read_bytes()).decode()
    size_mb = tmp.stat().st_size / 1024 / 1024
    tmp.unlink()
    return f"data:audio/mpeg;base64,{b64}", size_mb


def cmd_create(reference: str, yes: bool) -> None:
    ref = Path(reference)
    if not ref.exists():
        print(f"ERROR: 参照音声がありません: {ref}", file=sys.stderr)
        sys.exit(1)

    a = qc_audio.analyze(ref)
    if a["duration"] < 10:
        print(f"ERROR: 参照音声が短すぎます（{a['duration']:.1f}秒 / 10秒以上が必要）",
              file=sys.stderr)
        sys.exit(1)

    vid_path = BASE / "assets" / "voice_id.txt"
    print("ボイスクローンを作成します")
    print(f"  参照音声: {ref}  {a['duration']:.1f}秒  ピーク {a['peak']:.3f}")
    print(f"  モデル  : {CLONE_MODEL}")
    print(f"  テスト文: {TEST_TEXT}")
    if vid_path.exists():
        print(f"\n  ⚠️ すでに voice_id.txt があります（上書きされます）")
        print("     既存の検証済みクリップは声が変わるため作り直しが必要になります")
    print("\n  ※ ここから fal への課金が発生します。")
    if not yes:
        try:
            ans = input("  続けますか？ [y/N]: ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("  中止しました。")
            return

    uri, size_mb = to_data_uri(ref)
    print(f"\n  参照音声を送信中（mp3に変換して {size_mb:.1f}MB）...")

    def on_status(state, elapsed):
        print(f"    [{elapsed:5.1f}s] {state}", flush=True)

    result = fal_client.run(CLONE_MODEL, {
        "audio_url": uri,
        # 前処理で正規化・ノイズ確認まで済ませてあるので、fal側では触らせない。
        # 二重に掛けると声の質感が変わる。
        "noise_reduction": False,
        "need_volume_normalization": False,
        "text": TEST_TEXT,
        "model": "speech-02-hd",
    }, on_status=on_status)

    voice_id = result.get("custom_voice_id")
    if not voice_id:
        raise RuntimeError(f"応答に custom_voice_id がありません: {str(result)[:400]}")

    vid_path.write_text(voice_id + "\n", encoding="utf-8")
    vid_path.chmod(0o600)
    print(f"\n  ✅ クローン作成完了 → assets/voice_id.txt に保存しました")

    # 7日以内にTTSで使わないと消える仕様。プレビュー音声がその1回にあたるが、
    # 手元にも残して検収できるようにする。
    prev_url = (result.get("audio") or {}).get("url") or result.get("audio_url")
    out = PREP_DIR / "clone_test.wav"
    if not prev_url:
        print(f"     （応答に含まれていたキー: {', '.join(sorted(result))}）", file=sys.stderr)
    if prev_url:
        raw = PREP_DIR / "clone_test.download"
        fal_client.download(prev_url, raw)
        run(["ffmpeg", "-y", "-i", str(raw), "-ac", "1", "-c:a", "pcm_s16le", str(out)])
        raw.unlink()
        t = qc_audio.analyze(out)
        print(f"  ✅ テスト生成も完了 → {out.relative_to(BASE)}  {t['duration']:.1f}秒")
        print(f"     （7日以内に一度使わないとクローンは自動削除されます。これで条件を満たしました）")
    else:
        print("  ⚠️ プレビュー音声が返りませんでした。", file=sys.stderr)
        print("     tools/tts.py で一度生成してください（7日以内に使わないと消えます）",
              file=sys.stderr)

    try:
        ref_name = str(ref.resolve().relative_to(BASE))
    except ValueError:
        ref_name = str(ref)   # リポジトリ外の音声を渡された場合はそのまま記録する
    idx = {"created": datetime.date.today().isoformat(),
           "model": CLONE_MODEL, "reference": ref_name,
           "reference_sec": round(a["duration"], 2)}
    (BASE / "assets" / "voice" / "clone_info.json").write_text(
        json.dumps(idx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("\n  次にやること:")
    print("    1. assets/voice/prep/clone_test.wav を聴いて、本人の声に聞こえるか確認")
    print("    2. assets/tts_config.json の \"engine\" を \"minimax\" に変更")
    print("    3. python3 tools/qc_audio.py --measure で検収しきい値を測り直す")
    print("    4. 検証済みクリップを新しい声で作り直す")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ボイスクローンの参照音声づくりとクローン作成")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prep", help="参照音声の前処理（処理前後を両方残す）")
    p.add_argument("sources", nargs="+", help="ボイスメモ等の音声ファイル（複数なら結合）")
    p.add_argument("--name", default="reference", help="出力の名前（既定: reference）")
    p.add_argument("--denoise", action="store_true",
                   help="ノイズ除去を掛ける（暗騒音が実際に乗っている録音にだけ使う）")

    c = sub.add_parser("create", help="クローンを作る（課金あり・作成直後にテスト生成）")
    c.add_argument("reference", help="前処理済みの参照音声")
    c.add_argument("-y", "--yes", action="store_true", help="確認を省く")

    a = ap.parse_args()
    try:
        if a.cmd == "prep":
            cmd_prep(a.sources, a.name, a.denoise)
        elif a.cmd == "create":
            cmd_create(a.reference, a.yes)
    except RuntimeError as e:
        print(f"\n❌ {e}", file=sys.stderr)
        sys.exit(1)
