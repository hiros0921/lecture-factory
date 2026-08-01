#!/usr/bin/env python3
"""公開用リポジトリを組み立てる。

    python3 tools/make_public.py <出力先>

手元のリポジトリをそのまま公開してはいけない。最新のコミットで消しても
履歴に残るため、講座の台本26本すべてが復元できてしまう。
そこで公開対象だけを新しいフォルダにコピーし、履歴ごと作り直す。

仕様書の「配布用フォルダを分離し、出荷前に1ファイルずつ内容を確認する」
という要件に対応する。ホワイトリスト方式（入れるものを列挙する）にしてあり、
新しいファイルが増えても勝手に混入しない。
"""
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

# 公開するものだけを列挙する。ここに書いていないものは入らない。
INCLUDE = [
    "pipeline.py",
    ".gitignore",
    "tools/assemble.py",
    "tools/fal_client.py",
    "tools/fixed_clips.py",
    "tools/make_cam.py",
    "tools/make_public.py",
    "tools/make_slides.py",
    "tools/parse_script.py",
    "tools/qc_audio.py",
    "tools/qc_transcribe.py",
    "tools/telop.py",
    "tools/tts.py",
    "tools/upload_r2.py",
    "tools/verify_take.py",
    "tools/voice_clone.py",
    "templates/slide_body.html",
    "templates/slide_title.html",
    "assets/qc_aliases.json",
    "assets/tts_config.json",
    "assets/yomi_dict.json",
    "docs/reference_recording.md",
    # 台本は形式が分かる3本だけ。残り23本は講座の中身なので出さない。
    "scripts/lesson01.md",
    "scripts/lesson02.md",
    "scripts/lesson03.md",
]

# 入っていたら事故。push 前に必ず検査する。
FORBIDDEN = [
    ".fal_key", "voice_id", "r2_config", "access_config",
    "assets/voice", "worker/", "article_draft",
]


def check_forbidden(dst: Path) -> list:
    hits = []
    for p in sorted(dst.rglob("*")):
        if p.is_dir() or ".git/" in str(p):
            continue
        rel = str(p.relative_to(dst))
        for bad in FORBIDDEN:
            if bad in rel:
                hits.append(rel)
        # 中身にキーらしき文字列が無いかも見る
        try:
            body = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        import re
        if re.search(r"[0-9a-f]{8}-[0-9a-f-]{27}:[0-9a-f]{32}", body):
            hits.append(f"{rel} ← APIキーらしき文字列")
    return hits


def main(out: str) -> None:
    dst = Path(out).expanduser().resolve()
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    missing = [f for f in INCLUDE if not (BASE / f).exists()]
    if missing:
        print("ERROR: 見つからないファイル:", ", ".join(missing), file=sys.stderr)
        sys.exit(1)

    for f in INCLUDE:
        target = dst / f
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(BASE / f, target)

    hits = check_forbidden(dst)
    print(f"公開用を組み立てました: {dst}")
    print(f"  ファイル数: {sum(1 for p in dst.rglob('*') if p.is_file())}")
    if hits:
        print("\n❌ 入ってはいけないものが混入しています:", file=sys.stderr)
        for h in hits:
            print(f"     {h}", file=sys.stderr)
        sys.exit(1)
    print("  ✅ 秘密・非公開対象の混入なし")

    excluded = subprocess.run(["git", "ls-files"], cwd=BASE,
                              capture_output=True, text=True).stdout.split()
    left_out = [f for f in excluded if f not in INCLUDE]
    print(f"\n  公開しないもの（{len(left_out)}件）:")
    for f in left_out:
        print(f"     {f}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: make_public.py <出力先フォルダ>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
