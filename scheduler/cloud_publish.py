"""GitHub Actions에서 queue/*.json 예약 게시물을 시각이 되면 Instagram에 올린다.

예약 파일은 MODS 공유 드라이브의 05_인스타발행/ig_publish.py schedule 이 만든다.
  {"id", "account": "global|thailand", "publish_at": UTC ISO, "image_urls": [...],
   "caption", "folder", "status": "pending|publishing|done|failed", ...}
토큰은 저장소 Secrets IG_TOKEN_GLOBAL · IG_TOKEN_THAILAND.
DRY_RUN=true 면 토큰·이미지 주소만 확인하고 게시하지 않는다.
"""
import datetime as dt
import glob
import json
import os
import subprocess
import sys
import time

import requests

API = "https://graph.instagram.com/v23.0"
USERS = {"global": "mods.global", "thailand": "mods.thailand"}
DRY = os.environ.get("DRY_RUN", "").lower() == "true"


def token(acct):
    t = os.environ.get(f"IG_TOKEN_{acct.upper()}", "").strip()
    if not t:
        raise RuntimeError(f"Secret IG_TOKEN_{acct.upper()} 가 없습니다")
    return t


def call(acct, method, path, **params):
    params["access_token"] = token(acct)
    data = requests.request(method, f"{API}/{path}", params=params, timeout=60).json()
    if "error" in data:
        e = data["error"]
        raise RuntimeError(f"API 오류 ({e.get('code')}): {e.get('message')}")
    return data


def me(acct):
    m = call(acct, "GET", "me", fields="user_id,username")
    if m["username"] != USERS[acct]:
        raise RuntimeError(f"토큰 계정이 다릅니다: {acct} 는 @{USERS[acct]} 인데 토큰은 @{m['username']}")
    return m


def wait_ready(acct, cid):
    for _ in range(100):
        s = call(acct, "GET", cid, fields="status_code,status")
        if s["status_code"] == "FINISHED":
            return
        if s["status_code"] in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"컨테이너 실패: {s}")
        time.sleep(3)
    raise RuntimeError("컨테이너 준비 시간 초과")


def publish(item):
    acct, urls, caption = item["account"], item["image_urls"], item["caption"]
    uid = me(acct)["user_id"]
    if len(urls) == 1:
        cid = call(acct, "POST", f"{uid}/media", image_url=urls[0], caption=caption)["id"]
    else:
        kids = []
        for u in urls:
            k = call(acct, "POST", f"{uid}/media", image_url=u, is_carousel_item="true")["id"]
            wait_ready(acct, k)
            kids.append(k)
        cid = call(acct, "POST", f"{uid}/media", media_type="CAROUSEL",
                   children=",".join(kids), caption=caption)["id"]
    wait_ready(acct, cid)
    mid = call(acct, "POST", f"{uid}/media_publish", creation_id=cid)["id"]
    return call(acct, "GET", mid, fields="permalink").get("permalink")


def save(path, item, msg):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(item, f, ensure_ascii=False, indent=2)
    subprocess.run(["git", "add", path], check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], check=True)
    for _ in range(5):  # 다른 커밋과 겹치면 다시 받아서 올린다
        subprocess.run(["git", "pull", "-q", "--rebase"], check=False)
        if subprocess.run(["git", "push", "-q"]).returncode == 0:
            return
        time.sleep(5)
    raise RuntimeError("git push 실패")


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def main():
    now = dt.datetime.now(dt.timezone.utc)
    failed = False
    for path in sorted(glob.glob("queue/*.json")):
        with open(path, encoding="utf-8") as f:
            item = json.load(f)
        if item.get("status") != "pending":
            continue
        due = dt.datetime.fromisoformat(item["publish_at"]) <= now
        label = f"{item['id']} (@{USERS[item['account']]}, {item['publish_at']})"
        if DRY:
            try:
                me(item["account"])
                bad = [u for u in item["image_urls"] if requests.head(u, timeout=20).status_code != 200]
                print(f"[dry] {label}: 토큰 OK, 이미지 {len(item['image_urls'])}장"
                      + (f", 열리지 않는 주소 {bad}" if bad else " 모두 열림")
                      + (", 게시 시각 지남" if due else ""))
                failed |= bool(bad)
            except Exception as e:
                print(f"[dry] {label}: {e}")
                failed = True
            continue
        if not due:
            continue
        # 먼저 publishing 으로 표시해 두 번 올라가지 않게 한다 (중간에 멈추면 사람이 확인)
        item["status"] = "publishing"
        item["started_at"] = now_iso()
        save(path, item, f"publishing {item['id']}")
        try:
            item["permalink"] = publish(item)
            item["status"] = "done"
            item["published_at"] = now_iso()
            print(f"게시 완료 {label}: {item['permalink']}")
        except Exception as e:
            item["status"] = "failed"
            item["error"] = str(e)
            failed = True
            print(f"게시 실패 {label}: {e}")
        save(path, item, f"{item['status']} {item['id']}")
    sys.exit(1 if failed else 0)  # 실패하면 GitHub이 메일로 알린다


if __name__ == "__main__":
    main()
