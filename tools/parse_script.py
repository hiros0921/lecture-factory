#!/usr/bin/env python3
"""台本(markdown)をパースして build/lessonNN/segments.json に変換する。

台本フォーマット:
    # 第N回：タイトル
    ## とびら: 大見出し        ← 章とびらスライド
    ## 見出し                  ← 本文スライド
    - 箇条書き（最大4点）
    ---
    ナレーション本文
"""
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
MAX_BULLETS = 4
# テロップの強調（**…**）は1本あたりこの数まで。増やすほど「どこも強調されていない」
# のと同じになる。色は telop.py 側で金1色に固定されている。
MAX_EMPHASIS = 3


def parse_script(md_path: Path) -> dict:
    text = md_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    lesson_title = ""
    segments = []
    cur = None          # 現在のセグメント
    in_narration = False
    warnings = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            lesson_title = stripped[2:].strip()
        elif stripped.startswith("## "):
            if cur:
                segments.append(cur)
            heading = stripped[3:].strip()
            seg_type = "body"
            if heading.startswith("とびら:") or heading.startswith("とびら："):
                seg_type = "title"
                heading = re.sub(r"^とびら[:：]\s*", "", heading)
            cur = {"type": seg_type, "heading": heading, "bullets": [], "narration": ""}
            in_narration = False
        elif stripped == "---":
            in_narration = True
        elif cur is not None:
            if in_narration:
                cur["narration"] += line + "\n"
            elif stripped.startswith("- "):
                cur["bullets"].append(stripped[2:].strip())

    if cur:
        segments.append(cur)

    # 整形と検収
    for i, seg in enumerate(segments, 1):
        seg["id"] = f"seg{i:02d}"
        seg["narration"] = seg["narration"].strip()
        if len(seg["bullets"]) > MAX_BULLETS:
            warnings.append(f"{seg['id']}: 箇条書きが{len(seg['bullets'])}点あります（推奨は最大{MAX_BULLETS}点）")
        if not seg["narration"]:
            warnings.append(f"{seg['id']}: ナレーションが空です")

    total_chars = sum(len(s["narration"]) for s in segments)
    if not segments:
        warnings.append("セグメントが1つもありません（## 見出し が必要）")

    # 強調（**…**）の数を数える。テロップの強調は金1色に固定されているので、
    # 色数は増えないが、箇所が増えると強調の意味がなくなる。
    emphases = [(s["id"], m) for s in segments
                for m in re.findall(r"\*\*(.+?)\*\*", s["narration"])]
    if len(emphases) > MAX_EMPHASIS:
        warnings.append(
            f"強調（**…**）が{len(emphases)}箇所あります（推奨は1本あたり{MAX_EMPHASIS}箇所まで）: "
            + " / ".join(f"{sid}「{t}」" for sid, t in emphases[:6])
            + (" ..." if len(emphases) > 6 else ""))

    return {
        "title": lesson_title,
        "segments": segments,
        "total_narration_chars": total_chars,
        "emphasis_count": len(emphases),
        "warnings": warnings,
    }


def main(lesson: str) -> dict:
    md_path = BASE / "scripts" / f"lesson{lesson}.md"
    if not md_path.exists():
        print(f"ERROR: 台本が見つかりません: {md_path}", file=sys.stderr)
        sys.exit(1)

    data = parse_script(md_path)
    out_dir = BASE / "build" / f"lesson{lesson}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "segments.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"台本パース完了: {data['title']}")
    print(f"  セグメント数: {len(data['segments'])} / ナレーション合計: {data['total_narration_chars']}字"
          f" / 強調: {data['emphasis_count']}箇所（金1色・推奨{MAX_EMPHASIS}箇所まで）")
    for w in data["warnings"]:
        print(f"  ⚠️  {w}")
    print(f"  → {out_path.relative_to(BASE)}")
    return data


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: parse_script.py <lesson番号 例:01>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
