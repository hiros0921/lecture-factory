# lecture-factory

撮影ゼロでAI動画講義を量産するパイプライン。

## 使い方

```bash
# 一気に全工程（台本→スライド→音声→組み立て→R2アップロード）
python3 pipeline.py 01

# 本番に上げずに手元で確認したいとき（r2_config.json がある場合、
# 引数なしだとアップロードまで走るので注意）
python3 pipeline.py 01 --skip-upload

# 部分実行
python3 pipeline.py 01 --step slides    # スライドだけ
python3 pipeline.py 01 --step tts       # 音声だけ（検収つき）
python3 pipeline.py 01 --step qc        # 音声を作り直さず照合だけやり直す
python3 pipeline.py 01 --step assemble  # 組み立てだけ
python3 pipeline.py 01 --step upload    # R2アップロードだけ（要APIキー）
```

## 必要なもの

- ffmpeg / ffprobe（`brew install ffmpeg`）
- Google Chrome（スライドとテロップの描画に使う）
- Python パッケージ: `python3 -m pip install pykakasi mlx-whisper`
  - `pykakasi` … 書き起こし照合の表記ゆれ吸収に必須
  - `mlx-whisper` … Apple Silicon 用の高速版 whisper（無い場合は
    openai-whisper のCLIに自動で切り替わるが、9倍ほど遅くなる）

## フォルダ構成

- `scripts/` … 台本（lesson01.md 形式）← あなたが書く場所
- `slides/` … 生成されたスライドHTMLとPNG
- `audio/` … 生成された音声wav
- `out/` … 完成したmp4
- `assets/` … 読み替え辞書・参照音声・APIキー等
- `tools/` … 各工程のスクリプト（Claude Codeが管理）
- `templates/` … スライドHTMLテンプレート
- `build/` … 中間ファイル（台本のパース結果など）

## 台本の書き方（scripts/lessonNN.md）

```markdown
# 第1回：講座タイトル

## とびら: 第1回　講座タイトル
---
ここにナレーション。とびらスライドは大見出しだけが表示されます。

## セグメントの見出し
- 箇条書きポイント1
- 箇条書きポイント2
---
ここにこのセグメントのナレーション。話し言葉で書く。
```

ルール:
- `## ` で1セグメント。`とびら: ` で始めると章とびらスライドになる
- `---` の上がスライド内容、下がナレーション
- 箇条書きは最大4点まで（それ以上は自動検収で警告）
- `**強調**` はテロップで金色になる。1本あたり3箇所まで（超えると警告）
- コード・URL・記号は読み上げない。「詳しくはテキストに」と誘導

## TTSエンジン

`assets/tts_config.json` で切り替え:
- `"engine": "say"` … macOS標準Kyoko（無料・テスト用）
- `"engine": "minimax"` … fal経由MiniMax（要 `assets/.fal_key` と `assets/voice_id.txt`）

## 音声の自動検収

音声は作りっぱなしにせず、2段階で機械的に検査する。落ちたら設定を変えて
最大3回まで自動でリトライし、それでも直らなければ理由を出して止まる。

**B: 波形の検収（`tools/qc_audio.py`）** … 短すぎ／長すぎ／尻切れ／音割れ／無音落ち

```bash
python3 tools/qc_audio.py --measure audio/lesson01/*.wav   # 数値の分布を見る
```

しきい値は `assets/tts_config.json` のエンジン別 `qc` にある。
**エンジンを MiniMax に切り替えたら、必ず `--measure` で取り直すこと。**
波形の性質が変わるので、say 用の値のままだと正常な音声が落ちる。

**C: 書き起こし照合（`tools/qc_transcribe.py`）** … 読み間違い・語尾のイントネーション

whisper で書き起こして原文と比べる。「ぼく↔僕」のような表記ゆれは
ひらがなに正規化して吸収する。

```bash
python3 tools/qc_transcribe.py --self-test   # 音声なしで正規化ロジックを検査
python3 tools/qc_transcribe.py 01            # lesson01 の音声を照合
```

不合格になったら、報告に出る「読ませた文」と「whisperの聞取」を見比べる。
**音として正しく読めているのに落ちている場合がある**（whisper は人名・地名を
よく聞き間違える）。その場合は音声に問題はないので、そのまま採用してよい。

## 画像・動画の自動検収

生成AIは黙って失敗する。画像は真っ黒なまま返り、動画は1フレームも動かないことがある。
どちらも生成そのものは成功扱いなので、API のエラーでは検出できない。
そして 0.3秒で切り替わるカットに使うと、目視ではまず気づけない。だから機械で測る。

**画像（`tools/qc_visual.py`）** … 被写体が黒に沈んだ絵を検出する

```bash
python3 tools/qc_visual.py --calibrate approved/*.png   # 合格ラインを実測する
python3 tools/qc_visual.py --min 0.46 images/*.png      # 検査する
```

グレースケールで測ってはいけない。輝度は赤の重みが 0.30 しかないため、
赤が支配的な絵が不当に暗いと判定される。RGB の最大値で測る。

較正には**似た種類のカットだけ**を渡すこと。演出上わざと真っ暗にしたカットを
正解に含めると、不良の真っ暗な絵と区別がつかなくなる。

**動画（`tools/qc_motion.py`）** … 静止したまま返ってきたクリップを検出する

```bash
python3 tools/qc_motion.py --calibrate approved/*.mp4
python3 tools/qc_motion.py --min 16.70 clips/*.mp4
```

判定は三分岐にしてある。二者択一にすると、ゆっくりしたカメラワークを
不良と誤判定するため。

```
合格      下限以上          そのまま使う
要確認    1.0 〜 下限       カメラワークかもしれない。コマを並べて目で見る
不良      1.0 未満          本当に動いていない。作り直す
```

**要確認のものを、目視せずに作り直してはいけない。** この指標はコヒーレントな
平行移動を過小評価する。実際、MV制作時に不合格とした7本のうち4本は、
目で見ると十分動いていた。指標を信じ切っていれば不要な生成に $1.40 使っていた。

## 固有名詞が何度やっても直らないとき

読み替え辞書では根治しないことがある。その場合は「一度だけ完璧に読めた
テイク」を作って使い回す。

```bash
python3 tools/verify_take.py "みなさん、こんにちは。諏訪です。" greeting
python3 tools/fixed_clips.py --list    # 登録済みクリップの一覧
python3 tools/fixed_clips.py --check   # 今の声と合っているか確認
```

以後、この文で始まるナレーションは TTS に読ませず、クリップ + 0.25秒の間 +
残りのTTS を結合して作る。

**声を変えたら（クローン作成・voice_id 変更）クリップは作り直しが必要。**
合わないまま実行すると、声が混ざらないようエラーで止まる。

## AI音声の注記

AI音声を使った動画には、その旨を明記する取り決めにしている。
全スライドのフッターに「音声はAIで生成しています」が入る。
文言は `assets/slide_config.json` の `ai_notice` で変えられるが、
空にすると警告が出る。

## APIキーの置き場所（コードには絶対書かない）

- `assets/.fal_key` … falのAPIキー（1行）
- `assets/voice_id.txt` … ボイスクローンのID
- `assets/r2_config.json` … R2のバケット名等
- `assets/access_config.json` … 視聴ゲートのURLと合言葉
- `assets/voice/verified/` … 検証済みクリップ（本人の声）

これらはすべて `.gitignore` 済み。**配布物に混ぜないこと。**
出荷前に配布用フォルダを分け、1ファイルずつ中身を確認する。
