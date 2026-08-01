#!/usr/bin/env python3
"""動画講義 量産パイプライン。

    python3 pipeline.py 01               # 全工程（台本→スライド→TTS→組み立て→R2）
    python3 pipeline.py 01 --step slides # 部分実行
    python3 pipeline.py 01 --skip-upload # アップロードを飛ばす

    python3 pipeline.py 03 04 06         # 複数まとめて
    python3 pipeline.py --all            # scripts/ にある全部
    python3 pipeline.py --all --skip-done # まだ動画がない回だけ

1本だけの実行は、失敗したらその場で止まる（原因を見てから進みたいため）。
複数本の実行は、失敗した回を飛ばして最後まで進み、終わりに一覧で報告する。
26本を回すときに3本目で止まって残りが未着手、という状態を避けるため。
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
TOOLS = BASE / "tools"

STEPS = [
    ("parse",    "台本パース",       "parse_script.py"),
    ("slides",   "スライド生成",     "make_slides.py"),
    ("tts",      "音声生成(検収つき)", "tts.py"),
    ("qc",       "書き起こし照合",   "qc_transcribe.py"),
    ("assemble", "動画組み立て",     "assemble.py"),
    ("upload",   "R2アップロード",   "upload_r2.py"),
]

# 全工程を通すときは飛ばす工程。qc は tts.py の検収ループの中で既に走っているため、
# 通し実行で回すと同じ照合を二度やることになる。--step qc で単独実行するためだけに置く
# （音声を作り直さずに照合だけやり直したいときに使う）。
FULL_RUN_SKIP = {"qc"}


class StepFailed(Exception):
    """工程が失敗したことを、呼び出し元に伝える。"""

    def __init__(self, step: str, label: str, code: int):
        self.step, self.label, self.code = step, label, code
        super().__init__(label)


def run_step(name: str, label: str, script: str, lesson: str) -> None:
    print(f"\n━━━ {label} ({script}) ━━━", flush=True)
    t0 = time.time()
    r = subprocess.run([sys.executable, str(TOOLS / script), lesson])
    if r.returncode != 0:
        raise StepFailed(name, label, r.returncode)
    print(f"（{time.time() - t0:.1f}秒）")


def run_lesson(lesson: str, todo: list) -> float:
    """1回分を通す。失敗したら StepFailed を投げる。"""
    t0 = time.time()
    for name, label, script in todo:
        run_step(name, label, script, lesson)
    return time.time() - t0


def resolve_lessons(args) -> list:
    if args.all:
        found = sorted(p.stem.replace("lesson", "")
                       for p in (BASE / "scripts").glob("lesson*.md"))
        if args.skip_done:
            found = [n for n in found if not (BASE / "out" / f"lesson{n}.mp4").exists()]
        return found
    return [x.zfill(2) for x in args.lesson]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("lesson", nargs="*", help="回番号（例: 01）。複数指定できる")
    ap.add_argument("--all", action="store_true", help="scripts/ にある全部を対象にする")
    ap.add_argument("--skip-done", action="store_true",
                    help="--all のとき、すでに動画がある回を飛ばす")
    ap.add_argument("--step", choices=[s[0] for s in STEPS], help="この工程だけ実行")
    ap.add_argument("--skip-upload", action="store_true", help="R2アップロードを飛ばす")
    args = ap.parse_args()

    lessons = resolve_lessons(args)
    if not lessons:
        ap.error("回番号を指定してください（または --all）")

    todo = ([s for s in STEPS if s[0] == args.step] if args.step
            else [s for s in STEPS if s[0] not in FULL_RUN_SKIP])

    # アップロードは設定がある時だけ（未設定ならスキップ扱い）
    r2_ready = (BASE / "assets" / "r2_config.json").exists()
    if not args.step:
        if args.skip_upload or not r2_ready:
            todo = [s for s in todo if s[0] != "upload"]
            if not r2_ready and not args.skip_upload:
                print("※ assets/r2_config.json が未設定のため、アップロード工程はスキップします", flush=True)

    uploaded = any(s[0] == "upload" for s in todo)
    single = len(lessons) == 1

    if not single:
        print(f"■ {len(lessons)}本をまとめて実行します: {', '.join(lessons)}", flush=True)

    started = time.time()
    results = []
    for i, lesson in enumerate(lessons, 1):
        if not single:
            print(f"\n{'█' * 3} [{i}/{len(lessons)}] lesson{lesson} {'█' * 3}", flush=True)
        try:
            elapsed = run_lesson(lesson, todo)
        except StepFailed as e:
            sys.stdout.flush()
            print(f"\n❌ lesson{lesson} の工程「{e.label}」で失敗しました"
                  f"（終了コード{e.code}）。", file=sys.stderr)
            print(f"   修正後の再開: python3 pipeline.py {lesson} --step {e.step}",
                  file=sys.stderr)
            sys.stderr.flush()
            results.append((lesson, None, e.label))
            if single:
                sys.exit(e.code)
            continue   # まとめて実行のときは、次の回へ進む

        results.append((lesson, elapsed, None))
        print(f"\n{'=' * 40}")
        print(f"✅ lesson{lesson} 完了（合計 {elapsed:.1f}秒）")
        report(lesson, uploaded)

    if not single:
        summary(results, time.time() - started, uploaded)
        if any(r[2] for r in results):
            sys.exit(1)


def summary(results: list, total: float, uploaded: bool) -> None:
    ok = [r for r in results if r[2] is None]
    ng = [r for r in results if r[2] is not None]

    print(f"\n\n{'═' * 62}")
    print(f"■ まとめ　成功 {len(ok)}本 / 失敗 {len(ng)}本"
          f"（全体 {int(total // 60)}分{int(total % 60):02d}秒）")
    print("═" * 62)
    print(f"{'回':<6}{'状態':<8}{'長さ':>9}{'サイズ':>10}   生成時間")
    print("-" * 62)
    for lesson, elapsed, err in results:
        mp4 = BASE / "out" / f"lesson{lesson}.mp4"
        if err:
            print(f"{lesson:<6}{'❌ 失敗':<8}{'—':>9}{'—':>10}   {err}")
            continue
        sec = probe_seconds(mp4)
        length = f"{int(sec // 60)}分{sec % 60:04.1f}秒" if sec else "—"
        size = f"{mp4.stat().st_size / 1024 / 1024:.1f}MB" if mp4.exists() else "—"
        print(f"{lesson:<6}{'✅ 完了':<8}{length:>9}{size:>10}   {elapsed:.0f}秒")

    if ok:
        total_sec = sum(probe_seconds(BASE / "out" / f"lesson{l}.mp4") for l, _, e in results if e is None)
        print("-" * 62)
        print(f"　完成した動画の合計時間: {int(total_sec // 60)}分{int(total_sec % 60):02d}秒")
    if ng:
        print(f"\n　失敗した回: {', '.join(l for l, _, e in results if e)}")
        print("　上のログで該当箇所のエラーを確認してください。")
        print("　再実行例: python3 pipeline.py " + " ".join(l for l, _, e in results if e))


def probe_seconds(mp4: Path) -> float:
    if not mp4.exists():
        return 0.0
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(mp4)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def report(lesson: str, uploaded: bool) -> None:
    """完成物の一覧を出す。長さ・サイズ・配信URLをここで確認できるようにする。"""
    mp4 = BASE / "out" / f"lesson{lesson}.mp4"
    if not mp4.exists():
        return

    sec = probe_seconds(mp4)
    length = f"{int(sec // 60)}分{sec % 60:04.1f}秒" if sec else "不明"

    print(f"   動画    : {mp4.relative_to(BASE)}")
    print(f"   長さ    : {length}")
    print(f"   サイズ  : {mp4.stat().st_size / 1024 / 1024:.1f}MB")

    # 配信URLは合言葉ゲート側の設定から組み立てる。合言葉そのものは表示しない。
    access_path = BASE / "assets" / "access_config.json"
    if access_path.exists():
        gate = json.loads(access_path.read_text(encoding="utf-8")).get("gate_url", "").rstrip("/")
        if gate:
            state = "" if uploaded else "（今回はアップロードしていないため未反映）"
            print(f"   視聴URL : {gate}/watch/{lesson}{state}")
            print(f"   講義一覧: {gate}/")


if __name__ == "__main__":
    main()
