#!/usr/bin/env python3
"""fal API の共通クライアント（キュー方式）。

fal は「投げる → 状態を見る → 結果を取る」のキュー方式。同期エンドポイント
(fal.run) もあるが、混雑時にタイムアウトするので使わない。

    from fal_client import run
    result = run("fal-ai/minimax/speech-02-hd", {"text": "こんにちは", ...})

APIキーは assets/.fal_key から読む。コードにも環境変数にも書かない。
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
QUEUE_HOST = "https://queue.fal.run"
POLL_INTERVAL = 1.5
DEFAULT_TIMEOUT = 600


def load_key() -> str:
    p = BASE / "assets" / ".fal_key"
    if not p.exists():
        raise RuntimeError(
            "assets/.fal_key がありません。\n"
            "  fal.ai のダッシュボード（Developer → API Keys）でキーを発行し、\n"
            "  ターミナルで次を実行してください:\n"
            "    printf 'キーを貼り付けてEnter: '; read -rs K; "
            "printf '%s\\n' \"$K\" > assets/.fal_key; chmod 600 assets/.fal_key; unset K")
    key = p.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError("assets/.fal_key が空です。キーを保存し直してください。")
    return key


RETRIES = 3
RETRY_WAIT = 3.0


def _request(url: str, method: str = "GET", payload: dict = None,
             timeout: int = 120, attempt: int = 1) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Key {load_key()}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:800]
        hint = ""
        if e.code in (401, 403):
            hint = "\n  → APIキーが違うか、権限がありません。キーを発行し直してください。"
        elif e.code == 402:
            hint = "\n  → 残高が足りません。fal.ai の Settings → Credits でチャージしてください。"
        elif e.code == 422:
            hint = "\n  → 送ったパラメータが仕様に合っていません。上の本文に該当箇所が出ています。"
        elif e.code == 429:
            hint = "\n  → 短時間に投げすぎです。少し待ってからやり直してください。"
        raise RuntimeError(f"fal API エラー HTTP {e.code}\n  {body}{hint}") from None
    except (urllib.error.URLError, OSError) as e:
        # 画像を data URI で送ると本文が数MBになり、途中で接続が切れることがある
        # （Broken pipe）。相手側の問題ではないので、少し待って投げ直す。
        # HTTPError は URLError の派生なので、そちらは下の except で扱う。
        if attempt < RETRIES:
            reason = getattr(e, "reason", e)
            print(f"    通信が切れました（{reason}）。{RETRY_WAIT:.0f}秒後に再試行 "
                  f"{attempt + 1}/{RETRIES}", flush=True)
            time.sleep(RETRY_WAIT)
            return _request(url, method, payload, timeout, attempt + 1)
        raise RuntimeError(f"fal に接続できません（{RETRIES}回試行）: "
                           f"{getattr(e, 'reason', e)}") from None



def run(model_id: str, payload: dict, timeout: int = DEFAULT_TIMEOUT,
        on_status=None) -> dict:
    """キューに投げて完了まで待ち、結果を返す。"""
    sub = _request(f"{QUEUE_HOST}/{model_id}", "POST", payload)
    status_url = sub.get("status_url")
    response_url = sub.get("response_url")
    if not status_url or not response_url:
        raise RuntimeError(f"fal の応答に status_url / response_url がありません: {str(sub)[:300]}")

    t0 = time.time()
    last = None
    while True:
        st = _request(status_url)
        state = st.get("status")
        if state != last:
            last = state
            if on_status:
                on_status(state, time.time() - t0)
        if state == "COMPLETED":
            break
        if state in ("FAILED", "CANCELLED", "ERROR"):
            raise RuntimeError(f"fal の処理が失敗しました（{state}）: {str(st)[:500]}")
        if time.time() - t0 > timeout:
            raise RuntimeError(f"fal の応答が {timeout}秒 を超えました（状態: {state}）")
        time.sleep(POLL_INTERVAL)

    return _request(response_url)


def download(url: str, dst: Path, timeout: int = 300) -> None:
    """fal が返した音声・画像のURLをファイルに落とす。"""
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            dst.write_bytes(res.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"生成物をダウンロードできません: {e}") from None


def check_key() -> None:
    """キーが読めるかだけ確認する（課金は発生しない）。"""
    key = load_key()
    masked = f"{'*' * (len(key) - 4)}{key[-4:]}"
    print(f"assets/.fal_key を読み込めました（{len(key)}文字 / 末尾のみ表示: {masked[-8:]}）")


if __name__ == "__main__":
    try:
        check_key()
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
