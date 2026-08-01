#!/usr/bin/env python3
"""Cloudflare R2 へアップロード（読み戻し照合つき）＋プレーヤーHTML生成。

- wrangler r2 object put --remote で本番書き込みを明示
- アップロード後に読み戻してサイズ照合。不一致ならエラー停止（"上げたつもり"事故防止）
- public_base_url があればシーク（Range/206）対応も確認
- 必要な設定: assets/r2_config.json
    {
      "bucket": "バケット名",
      "prefix": "lectures",
      "public_base_url": "https://pub-xxxx.r2.dev"  ← 任意（公開URL）
    }
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def run(cmd: list, timeout: int = 600) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"コマンド失敗: {' '.join(cmd[:8])}...\n{r.stderr[-800:]}")
    return r


def check_range_support(url: str) -> bool:
    """先頭100バイトだけRangeリクエストして206が返るか確認（シーク対応の確認）"""
    r = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "-H", "Range: bytes=0-99", url],
        capture_output=True, text=True, timeout=60,
    )
    return r.stdout.strip() == "206"


def make_player_html(lesson: str, video_url: str) -> Path:
    out = BASE / "out" / f"player_lesson{lesson}.html"
    out.write_text(f"""<!DOCTYPE html>
<html lang="ja">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>第{int(lesson)}回 講義動画</title>
<style>
  body {{ font-family: "Hiragino Sans", sans-serif; max-width: 960px; margin: 40px auto; padding: 0 16px;
         background: #10151f; color: #f2ede2; }}
  h1 {{ font-family: "Hiragino Mincho ProN", serif; font-weight: 600; letter-spacing: 0.06em; }}
  video {{ width: 100%; border-radius: 8px; }}
  .speed {{ margin-top: 14px; display: flex; gap: 8px; align-items: center; }}
  .speed span {{ color: #c8a86a; font-size: 14px; margin-right: 4px; }}
  .speed button {{
    font-size: 14px; padding: 6px 14px; cursor: pointer;
    background: transparent; color: #cfd3da;
    border: 1px solid rgba(200,168,106,0.45); border-radius: 999px;
  }}
  .speed button.active {{ background: #c8a86a; color: #10151f; font-weight: 600; }}
  .note {{ color: rgba(207,211,218,0.5); font-size: 13px; margin-top: 16px; }}
</style></head>
<body>
  <h1>第{int(lesson)}回 講義動画</h1>
  <video id="v" controls preload="metadata" src="{video_url}"></video>
  <div class="speed">
    <span>再生速度</span>
    <button data-r="0.75">0.75×</button>
    <button data-r="1" class="active">1×</button>
    <button data-r="1.25">1.25×</button>
    <button data-r="1.5">1.5×</button>
    <button data-r="2">2×</button>
  </div>
  <p class="note">※ 本講義の音声はAIで生成しています。</p>
  <script>
    const v = document.getElementById('v');
    document.querySelectorAll('.speed button').forEach(b => b.onclick = () => {{
      v.playbackRate = parseFloat(b.dataset.r);
      document.querySelectorAll('.speed button').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
    }});
  </script>
</body>
</html>
""", encoding="utf-8")
    return out


def main(lesson: str) -> None:
    cfg_path = BASE / "assets" / "r2_config.json"
    if not cfg_path.exists():
        print("ERROR: assets/r2_config.json がありません。", file=sys.stderr)
        print('  例: {"bucket": "my-bucket", "prefix": "lectures", "public_base_url": "https://pub-xxxx.r2.dev"}', file=sys.stderr)
        sys.exit(1)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    bucket = cfg["bucket"]
    prefix = cfg.get("prefix", "lectures").strip("/")

    mp4 = BASE / "out" / f"lesson{lesson}.mp4"
    if not mp4.exists():
        print(f"ERROR: {mp4} がありません（先に assemble.py を実行）", file=sys.stderr)
        sys.exit(1)

    key = f"{prefix}/lesson{lesson}.mp4"
    local_size = mp4.stat().st_size

    print(f"R2アップロード開始: {key}（{local_size / 1024 / 1024:.1f}MB）")
    run(["npx", "wrangler", "r2", "object", "put", f"{bucket}/{key}",
         "--file", str(mp4), "--remote",
         "--content-type", "video/mp4"])

    # 読み戻し照合（視聴者が実際に通る経路で確認するのが最も確実。
    # 注意: wrangler get は上書きしたキーに古いキャッシュを返すことがあるため使わない）
    public_base = cfg.get("public_base_url", "").rstrip("/")
    access_path = BASE / "assets" / "access_config.json"
    access = json.loads(access_path.read_text(encoding="utf-8")) if access_path.exists() else {}
    print("読み戻して照合中...")
    if access.get("gate_url"):
        # 会員限定ゲート経由で照合（合言葉→Cookie→サイズ確認）
        gate = access["gate_url"].rstrip("/")
        r = subprocess.run(
            ["curl", "-s", "-i", "-d", f"pw={access['password']}&redirect=/", f"{gate}/login"],
            capture_output=True, text=True, timeout=120)
        cookie = ""
        for line in r.stdout.splitlines():
            if line.lower().startswith("set-cookie:"):
                cookie = line.split(":", 1)[1].strip().split(";")[0]
        if not cookie:
            print("❌ ゲートにログインできません（合言葉が変わった場合は assets/access_config.json を更新）", file=sys.stderr)
            sys.exit(1)
        r = subprocess.run(
            ["curl", "-sI", "-H", f"Cookie: {cookie}",
             f"{gate}/video/lesson{lesson}.mp4?verify={int(time.time())}"],
            capture_output=True, text=True, timeout=120)
        remote_size = -1
        for line in r.stdout.splitlines():
            if line.lower().startswith("content-length:"):
                remote_size = int(line.split(":")[1].strip())
    elif public_base:
        # キャッシュ回避のクエリを付けてサイズ確認
        check_url = f"{public_base}/{key}?verify={int(time.time())}"
        r = subprocess.run(
            ["curl", "-sI", check_url], capture_output=True, text=True, timeout=120)
        remote_size = -1
        for line in r.stdout.splitlines():
            if line.lower().startswith("content-length:"):
                remote_size = int(line.split(":")[1].strip())
    else:
        with tempfile.TemporaryDirectory() as tmp:
            back = Path(tmp) / "back.mp4"
            run(["npx", "wrangler", "r2", "object", "get", f"{bucket}/{key}",
                 "--file", str(back), "--remote"])
            remote_size = back.stat().st_size
    if remote_size != local_size:
        print(f"❌ サイズ不一致! ローカル{local_size} / リモート{remote_size} — アップロード失敗の可能性", file=sys.stderr)
        sys.exit(1)
    print(f"  ✅ サイズ一致（{remote_size}バイト）")

    # 視聴URLの案内（会員限定ゲートがあればそちらを優先）
    if access.get("gate_url"):
        gate = access["gate_url"].rstrip("/")
        print(f"  視聴URL（合言葉つき）: {gate}/watch/{lesson}")
        print(f"  講義一覧: {gate}/")
    elif public_base:
        url = f"{public_base}/{key}"
        seek_ok = check_range_support(url)
        print(f"  配信URL: {url}")
        print(f"  シーク対応(Range/206): {'✅ OK' if seek_ok else '⚠️ 未対応または未公開'}")
        player = make_player_html(lesson, url)
        print(f"  プレーヤー: {player.relative_to(BASE)}")
    else:
        print("  （public_base_url 未設定のためプレーヤー生成はスキップ）")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: upload_r2.py <lesson番号 例:01>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
