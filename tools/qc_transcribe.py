#!/usr/bin/env python3
"""C: 書き起こし照合。生成音声をwhisperで書き起こし、読ませた原文と機械照合する。

Bが「音の物理的な不良」を見るのに対し、Cは「読み間違い」を見る。

    python3 tools/qc_transcribe.py 01          # lesson01 の全wavを照合
    python3 tools/qc_transcribe.py --self-test # 正規化の単体テスト

判定のしくみ:
- whisperは「ぼく↔僕」「一行↔1行」のように表記が揺れる。そのまま比較すると
  正常な音声が延々と不合格になるので、両方をひらがなに落としてから比べる。
- 完全一致ではなく類似度で判定する（長音・促音の揺れを許容するため）。
- 語尾が平板になった疑問形は、whisperが別の語として書き起こす傾向がある
  （「やってますか？」が平板だと「やってますが」になる）。差分が疑問文の
  語尾に当たっていたら、読み間違いではなくイントネーション不良として報告する。
"""
import argparse
import difflib
import json
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 合格ライン。1.0=完全一致。長音・促音の揺れを許容しつつ、単語の読み違いは弾く値。
SIMILARITY_OK = 0.92
# 差分ブロックの許容長。実測では正常音声の差分は最大1文字（送り仮名・助詞のゆれ）、
# 固有名詞の誤読は2文字だった。長文中の短い誤読は全体類似度を動かさないため、
# 類似度だけでなくこの長さでも判定する。
MAX_DIFF_BLOCK = 2
# 疑問文の語尾とみなす文字数（末尾からこの文字数を検査する）
QUESTION_TAIL_CHARS = 2

_KAKASI = None


# ---------------- 正規化（表記ゆれの吸収） ----------------

_DIGIT_KANJI = "〇一二三四五六七八九"


def _int_to_kanji(n: int) -> str:
    """0〜9999 の整数を漢数字にする（12 → 十二、120 → 百二十）。"""
    if n == 0:
        return "〇"
    out = ""
    for unit, mark in ((1000, "千"), (100, "百"), (10, "十")):
        d, n = divmod(n, unit)
        if d:
            out += ("" if d == 1 else _DIGIT_KANJI[d]) + mark
    if n:
        out += _DIGIT_KANJI[n]
    return out


def _numbers_to_kanji(s: str) -> str:
    """半角数字を漢数字に直す。読みへの変換は pykakasi に任せる。

    自前でかなに変換してはいけない。助数詞で音が変わるため、
    「1本」を「いちほん」と読んでしまい、原文の「一本（いっぽん）」と
    食い違って、正常な音声が不合格になる。「一つ」「二つ」も同様。
    漢数字に寄せておけば、pykakasi が助数詞を見て正しく読んでくれる。
    """
    def rep(m):
        whole, frac = m.group(1), m.group(2)
        n = int(whole)
        head = _int_to_kanji(n) if n <= 9999 else "".join(_DIGIT_KANJI[int(c)] for c in whole)
        if frac:
            return head + "点" + "".join(_DIGIT_KANJI[int(c)] for c in frac)
        return head
    return re.sub(r"(\d+)(?:[.．](\d+))?", rep, s)


_TAGGER = None


def _kata_to_hira(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def _to_hiragana(s: str) -> str:
    """文をひらがなに開く。形態素解析で、文脈に合った読みを取る。

    ここは照合の精度を左右する。台本は「あと」「ほう」とひらがなで書くのに、
    whisperは「後」「方」と漢字で書き戻す。文脈を見ない変換だと
    「後→のち」「通り→とうり」のように別の読みを当ててしまい、
    正常な音声が大量に不合格になる（実測で21本中15本が停止した）。

    fugashi(+unidic-lite) は前後の語を見て読みを決めるので、
    ひらがなで書いても漢字で書いても同じ読みになる。
    入っていない環境では pykakasi に落ちるが、精度は下がる。
    """
    global _TAGGER, _KAKASI
    if _TAGGER is None and _KAKASI is None:
        try:
            import fugashi
            _TAGGER = fugashi.Tagger()
        except (ImportError, RuntimeError):
            try:
                import pykakasi
            except ImportError:
                print("ERROR: 照合には形態素解析が必要です:\n"
                      "  python3 -m pip install fugashi unidic-lite", file=sys.stderr)
                sys.exit(1)
            _KAKASI = pykakasi.kakasi()
            print("※ fugashi が無いため pykakasi で代用します（照合の精度が下がります）",
                  file=sys.stderr)

    if _TAGGER is not None:
        out = []
        for w in _TAGGER(s):
            kana = w.feature.kana          # 読み（カタカナ）。記号や英字では None
            out.append(_kata_to_hira(kana) if kana else w.surface)
        return "".join(out)
    return "".join(item["hira"] for item in _KAKASI.convert(s))


# 読み上げに現れない記号・空白・句読点。比較前に落とす。
_DROP = re.compile(r"[、。，．,.!！?？・…‥「」『』（）()\[\]【】〈〉《》\"'’”—ー\-–—~〜\s]")


_READINGS = None


def _apply_readings(s: str) -> str:
    """読み替え辞書を大文字小文字を無視して適用する。

    tts.py の apply_yomi_dict と同じ辞書を使うが、照合では大小の区別を無くす必要がある。
    TTSには「エーアイ」と読ませているのに、whisperは「ai」と小文字で書き戻すため、
    完全一致の置換だと当たらず、正常な音声が不合格になってしまう。
    """
    global _READINGS
    if _READINGS is None:
        path = BASE / "assets" / "yomi_dict.json"
        d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        _READINGS = [(k, v) for k, v in d.items() if not k.startswith("_")]
        _READINGS.sort(key=lambda kv: len(kv[0]), reverse=True)  # 長い語から（部分一致の誤爆防止）
    for k, v in _READINGS:
        s = re.sub(re.escape(k), v, s, flags=re.IGNORECASE)
    return s


_ALIASES = None


def apply_aliases(heard: str) -> str:
    """書き起こし側の異表記を、原文の表記に寄せる（照合のときだけ使う）。

    whisperは同じ音でも別の漢字を当てる。その漢字を pykakasi が別の読みに
    変換すると、正常な音声が不合格になる。TTSには一切影響しない処理なので、
    読み替え辞書と違ってアクセントは崩れない。
    """
    global _ALIASES
    if _ALIASES is None:
        path = BASE / "assets" / "qc_aliases.json"
        d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        _ALIASES = [(k, v) for k, v in d.items() if not k.startswith("_")]
        _ALIASES.sort(key=lambda kv: len(kv[0]), reverse=True)
    for k, v in _ALIASES:
        heard = heard.replace(k, v)
    return heard


def normalize(s: str) -> str:
    """比較用の正規形にする: 全角半角統一 → 読み替え辞書 → 数字を読みに → ひらがな化 → 記号除去。

    読み替え辞書は原文と書き起こしの「両方」に適用する。辞書の値側にキーは
    含まれないので、原文に二重適用しても結果は変わらない。
    """
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("**", "")            # テロップ用の強調記号は読まれない
    s = _apply_readings(s)
    s = _numbers_to_kanji(s)
    s = _to_hiragana(s)
    s = _DROP.sub("", s)
    return s.lower()


# ---------------- 疑問文の語尾位置 ----------------

_SENT_SPLIT = re.compile(r"(?<=[。！？!?])")


def question_tail_zones(text: str) -> list:
    """原文のうち「疑問文の語尾」に当たる区間を、正規化後の文字位置で返す。

    返り値: [(開始index, 終了index), ...]
    """
    zones = []
    pos = 0
    for sent in _SENT_SPLIT.split(text):
        if not sent.strip():
            continue
        norm = normalize(sent)
        end = pos + len(norm)
        # 「？」で終わる、または「か。」「か」で終わる文を疑問文とみなす
        stripped = sent.strip()
        is_q = stripped.endswith(("？", "?")) or re.search(r"か[。、]?$", stripped) is not None
        if is_q and norm:
            zones.append((max(pos, end - QUESTION_TAIL_CHARS), end))
        pos = end
    return zones


def classify_diff(src_norm: str, heard_norm: str, zones: list) -> tuple:
    """差分を (読み間違いの一覧, イントネーション疑いの一覧, 最大ブロック長) に分ける。"""
    misread, intonation, longest = [], [], 0
    sm = difflib.SequenceMatcher(None, src_norm, heard_norm, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        size = max(i2 - i1, j2 - j1)
        detail = f"「{src_norm[i1:i2] or '(なし)'}」→「{heard_norm[j1:j2] or '(なし)'}」"
        if any(i1 < ze and i2 > zs for zs, ze in zones):
            intonation.append(detail)
        else:
            longest = max(longest, size)
            misread.append(f"{detail}({size}文字)")
    return misread, intonation, longest


# ---------------- whisper ----------------

WHISPER_CLI = "/Library/Frameworks/Python.framework/Versions/3.13/bin/whisper"
# small から large-v3-turbo に変更（2026-07-31）。実測でキャッシュ後の速度は
# ほぼ同じ（1.4〜2.2秒）なのに、書き起こしの精度が明確に上がる。
# small は「敬語→経語」「言い切った→良い切った」「一枚→1枚」のように
# 誤って書き起こし、そのたびに正常な音声が不合格になっていた。
MLX_MODEL = "mlx-community/whisper-large-v3-turbo"


def transcribe(wav: Path, backend: str = "auto") -> str:
    """音声を書き起こす。mlx（Apple Silicon最適化・約9倍速）を優先し、無ければCLI版。"""
    if backend in ("auto", "mlx"):
        try:
            import mlx_whisper
            r = mlx_whisper.transcribe(str(wav), path_or_hf_repo=MLX_MODEL, language="ja")
            return r["text"].strip()
        except ImportError:
            if backend == "mlx":
                raise RuntimeError("mlx_whisper が入っていません: python3 -m pip install mlx-whisper")
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(
            [WHISPER_CLI, str(wav), "--language", "ja", "--model", "small",
             "--output_format", "txt", "--output_dir", tmp, "--fp16", "False"],
            capture_output=True, text=True, timeout=900,
        )
        if r.returncode != 0:
            raise RuntimeError(f"whisper失敗: {r.stderr[-400:]}")
        txt = Path(tmp) / (wav.stem + ".txt")
        return txt.read_text(encoding="utf-8").strip() if txt.exists() else ""


# ---------------- 照合本体 ----------------

def check(wav: Path, source_text: str, backend: str = "auto") -> dict:
    """1ファイルを照合する。返り値の ng が空なら合格。"""
    heard = transcribe(wav, backend)
    src_norm = normalize(source_text)
    heard_norm = normalize(apply_aliases(heard))

    if not heard_norm:
        return {"ok": False, "heard": heard, "similarity": 0.0,
                "ng": ["書き起こしが空（無音の可能性）"]}

    sim = difflib.SequenceMatcher(None, src_norm, heard_norm, autojunk=False).ratio()
    zones = question_tail_zones(source_text)
    misread, intonation, longest = classify_diff(src_norm, heard_norm, zones)

    ng = []
    if intonation:
        ng.append("語尾のイントネーション疑い: " + " / ".join(intonation[:3]))
    if sim < SIMILARITY_OK:
        ng.append(f"読み間違い疑い（類似度{sim:.3f} < {SIMILARITY_OK}）: "
                  + " / ".join(misread[:3]))
    elif longest >= MAX_DIFF_BLOCK:
        # 長文中の固有名詞の誤読は全体類似度をほとんど動かさないので、こちらで拾う
        ng.append(f"固有名詞の誤読疑い（{longest}文字の食い違い）: " + " / ".join(misread[:3]))
    return {"ok": not ng, "heard": heard, "similarity": sim, "ng": ng,
            "misread": misread, "intonation": intonation, "longest_diff": longest}


def main_lesson(lesson: str, backend: str) -> None:
    seg_path = BASE / "build" / f"lesson{lesson}" / "segments.json"
    if not seg_path.exists():
        print("ERROR: 先に parse_script.py を実行してください", file=sys.stderr)
        sys.exit(1)
    from tts import apply_yomi_dict

    data = json.loads(seg_path.read_text(encoding="utf-8"))
    audio_dir = BASE / "audio" / f"lesson{lesson}"

    print(f"書き起こし照合 lesson{lesson}（合格ライン 類似度{SIMILARITY_OK}）")
    failed, missing = [], []
    for seg in data["segments"]:
        wav = audio_dir / f"{seg['id']}.wav"
        if not wav.exists():
            # 存在しない音声を「合格」に数えてはいけない。TTSが途中で
            # 止まった回を「全ファイル合格」と誤って報告してしまう。
            missing.append(seg["id"])
            print(f"  ❌ {seg['id']}.wav がありません（TTSが未完了）")
            continue
        # 比較対象は「実際に読ませたテキスト」＝読み替え辞書適用後
        r = check(wav, apply_yomi_dict(seg["narration"]), backend)
        if r["ok"]:
            print(f"  ✅ {seg['id']}.wav  類似度{r['similarity']:.3f}")
        else:
            failed.append(seg["id"])
            print(f"  ❌ {seg['id']}.wav  類似度{r['similarity']:.3f}")
            for n in r["ng"]:
                print(f"       {n}")
            print(f"       聞こえた: {r['heard'][:120]}")

    if failed or missing:
        sys.stdout.flush()
        if missing:
            print(f"\n❌ 音声が足りません: {', '.join(m + '.wav' for m in missing)}",
                  file=sys.stderr)
            print(f"   先に音声を作ってください: python3 pipeline.py {lesson} --step tts",
                  file=sys.stderr)
        if failed:
            print(f"\n❌ 照合に失敗: {', '.join(f + '.wav' for f in failed)}", file=sys.stderr)
            print("   「読ませた文」と「聞こえた」を見比べてください。音として正しく"
                  "読めているなら、whisperの聞き違いです。", file=sys.stderr)
            print("   その場合は assets/qc_aliases.json に書き起こし側の表記を"
                  "登録すれば直ります（TTSには影響しません）。", file=sys.stderr)
        sys.exit(1)
    print(f"全{len(data['segments'])}ファイル合格")


# ---------------- 単体テスト ----------------

SELF_TESTS = [
    # (原文, whisperの書き起こし, 合格すべきか, 説明)
    ("ぼくは、そう思います。", "僕はそう思います", True, "かな↔漢字のゆれ"),
    ("台本を一行直したら、その部分だけ作り直せる。", "台本を1行直したらその部分だけ作り直せる",
     True, "漢数字↔算用数字"),
    ("第3回は、全体の仕組みのお話です。", "第三回は全体の仕組みのお話です", True, "数字表記のゆれ"),
    ("スライドとナレーションつきの動画講義です。", "スライドとナレーション付きの動画講義です。",
     True, "送り仮名のゆれ"),
    ("費用は20円ほどです。", "費用は二十円ほどです", True, "十の位の読み"),
    ("撮影も録音も一切しないことです。", "撮影も6本も1歳しないことです", False, "明確な読み間違い"),
    ("まずこの講座のゴールから。", "まずこの高座のゴールから", True,
     "同音異義語（音は正しいのでwhisperの変換ミス。合格が正しい）"),
    ("お問い合わせは、電話でお願いします。", "お問い合わせはでん者でお願いします", False, "子音の誤読"),
    ("みなさんこんにちは。ライテックの諏訪です。", "皆さんこんにちはライテックのもろわです",
     False, "長文中の固有名詞の誤読（類似度0.92を超えるがブロック長で拾う）"),
    ("この講座では、AIだけで動画講義を作ります。", "この講座ではAIだけで動画講義を作ります",
     True, "英字略語（読み替え辞書を両側に適用）"),
]

INTONATION_TESTS = [
    ("動画の編集に、何時間かけていますか？", "動画の編集に何時間かけていますが", False, "疑問文の語尾が平板"),
    ("動画の編集に、何時間かけていますか？", "動画の編集に何時間かけていますか", True, "疑問文が正常"),
]


def self_test() -> None:
    print("=== 正規化と照合ロジックの単体テスト（音声生成なし） ===\n")
    ok_count = fail_count = 0

    print("[表記ゆれの吸収と誤読の検出]")
    for src, heard, should_pass, note in SELF_TESTS:
        sn, hn = normalize(src), normalize(heard)
        sim = difflib.SequenceMatcher(None, sn, hn, autojunk=False).ratio()
        _, _, longest = classify_diff(sn, hn, question_tail_zones(src))
        passed = sim >= SIMILARITY_OK and longest < MAX_DIFF_BLOCK
        mark = "✅" if passed == should_pass else "❌"
        if passed == should_pass:
            ok_count += 1
        else:
            fail_count += 1
        print(f"  {mark} 類似度{sim:.3f} 最大ブロック{longest} "
              f"期待={'合格' if should_pass else '不合格'} — {note}")
        if passed != should_pass:
            print(f"       原文正規形: {sn}")
            print(f"       聞取正規形: {hn}")

    print("\n[語尾のイントネーション検査]")
    for src, heard, should_pass, note in INTONATION_TESTS:
        sn, hn = normalize(src), normalize(heard)
        zones = question_tail_zones(src)
        _, intonation, _ = classify_diff(sn, hn, zones)
        passed = not intonation
        mark = "✅" if passed == should_pass else "❌"
        if passed == should_pass:
            ok_count += 1
        else:
            fail_count += 1
        print(f"  {mark} 期待={'合格' if should_pass else '不合格'} — {note}")
        if intonation:
            print(f"       検出: {' / '.join(intonation)}")

    print(f"\n結果: {ok_count}件成功 / {fail_count}件失敗")
    sys.exit(1 if fail_count else 0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("lesson", nargs="?", help="回番号（例: 01）")
    ap.add_argument("--self-test", action="store_true", help="正規化の単体テスト（音声不要）")
    ap.add_argument("--backend", default="auto", choices=["auto", "mlx", "cli"],
                    help="whisperの実行方式")
    a = ap.parse_args()
    if a.self_test:
        self_test()
    if not a.lesson:
        ap.error("回番号を指定してください（または --self-test）")
    main_lesson(a.lesson.zfill(2), a.backend)
