#!/usr/bin/env python3
"""追蹤台灣區 Apple TV (iTunes) 電影降價，並透過 Telegram Bot 推播。

資料來源是 Apple 官方的 iTunes RSS（itunes.apple.com/tw/rss/topmovies），
各分類排行榜的每部電影都附有售價、租價、封面與商品連結。
Apple 沒有提供「原價」欄位，所以每次執行都會記錄價格：
以「近期出現過的最高價」當作原價，現價明顯低於它就視為特價。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

STORE = "tw"
GENRES = [None, *range(4401, 4419), 4420, 4422, 4423, 4424]  # None = 總排行
FEED_URL = "https://itunes.apple.com/{store}/rss/topmovies/limit=200{genre}/json"

STATE_FILE = Path(os.environ.get("STATE_FILE") or Path(__file__).with_name("state.json"))
MIN_DISCOUNT = float(os.environ.get("MIN_DISCOUNT_PCT") or "20") / 100
MAX_NOTIFY = int(os.environ.get("MAX_NOTIFY") or "20")  # 單次最多推播幾部，其餘留到下次

REGULAR_AFTER_DAYS = 30  # 同一個價格維持這麼久，就視為新的正常售價
SEEN_REFRESH_DAYS = 30  # 「最後看到」日期的更新間隔（避免每天改動整份紀錄檔）
FORGET_AFTER_DAYS = 180  # 這麼久沒出現在榜單的電影，從紀錄中移除
TAIPEI = timezone(timedelta(hours=8))


def log(msg: str) -> None:
    print(msg, flush=True)


@dataclass
class Movie:
    id: str
    name: str
    price: int
    rental: int | None
    genre: str
    url: str
    image: str


@dataclass
class Deal:
    movie: Movie
    ref_price: int
    discount: float


# ---------- 抓取資料 ----------

def parse_entry(entry: dict) -> Movie | None:
    try:
        links = entry["link"] if isinstance(entry["link"], list) else [entry["link"]]
        url = next(l["attributes"]["href"] for l in links if l["attributes"].get("rel") == "alternate")
        rental = entry.get("im:rentalPrice")
        return Movie(
            id=entry["id"]["attributes"]["im:id"],
            name=entry["im:name"]["label"],
            price=round(float(entry["im:price"]["attributes"]["amount"])),
            rental=round(float(rental["attributes"]["amount"])) if rental else None,
            genre=entry["category"]["attributes"]["label"],
            url=url.split("?")[0],
            # RSS 附的是縮圖，換成 600x900 的 jpg（Telegram 才不會糊）
            image=re.sub(r"/\d+x\d+bb\.\w+$", "/600x900bb.jpg", entry["im:image"][-1]["label"]),
        )
    except (KeyError, IndexError, StopIteration, TypeError, ValueError):
        return None  # 沒有售價（例如只能租）或格式異常的項目直接略過


def fetch_movies(session: requests.Session) -> list[Movie]:
    movies: dict[str, Movie] = {}
    for genre in GENRES:
        url = FEED_URL.format(store=STORE, genre=f"/genre={genre}" if genre else "")
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            entries = resp.json()["feed"].get("entry") or []
        except (requests.RequestException, ValueError, KeyError) as e:
            log(f"  讀取分類 {genre or '總排行'} 失敗：{type(e).__name__}")
            continue
        if isinstance(entries, dict):  # 只有一筆時 Apple 會回傳 dict 而不是 list
            entries = [entries]
        for entry in entries:
            movie = parse_entry(entry)
            if movie:
                movies.setdefault(movie.id, movie)  # 同一部片會出現在多個分類
        time.sleep(0.3)
    if not movies:
        raise RuntimeError("沒有抓到任何電影，Apple RSS 可能暫時無法使用")
    return list(movies.values())


# ---------- 價格紀錄與特價判斷 ----------
# state: {電影ID: {name, ref(原價), price(目前價), since(此價開始日), seen, notified(已通知的價格)}}

def load_state() -> dict[str, dict]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict[str, dict]) -> None:
    # 一部電影一行，價格變動時 git diff 才好讀
    lines = [
        f"{json.dumps(k)}: {json.dumps(v, ensure_ascii=False, sort_keys=True)}"
        for k, v in sorted(state.items())
    ]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def age(iso: str, today: date) -> int:
    return (today - date.fromisoformat(iso)).days


def update_state(state: dict[str, dict], movies: list[Movie], today: date, min_discount: float) -> list[Deal]:
    """更新價格紀錄，回傳這次「新出現的」特價。第一次看到的電影只建立基準，不會通知。"""
    now = today.isoformat()
    deals = []
    for m in movies:
        rec = state.get(m.id)
        if rec is None:
            state[m.id] = {"name": m.name, "ref": m.price, "price": m.price,
                           "since": now, "seen": now, "notified": None}
            continue

        rec["name"] = m.name
        if age(rec["seen"], today) >= SEEN_REFRESH_DAYS:
            rec["seen"] = now
        if m.price != rec["price"]:
            rec["price"], rec["since"] = m.price, now
        rec["ref"] = max(rec["ref"], m.price)
        if m.price < rec["ref"] and age(rec["since"], today) >= REGULAR_AFTER_DAYS:
            rec["ref"] = m.price  # 長期維持這個價格，它就是新的正常售價

        if m.price >= rec["ref"]:
            rec["notified"] = None  # 回到原價，下次再降價可以重新通知
            continue
        discount = 1 - m.price / rec["ref"]
        # 還沒通知過，或是比上次通知時又更便宜，才算新特價
        if discount >= min_discount and (rec["notified"] is None or m.price < rec["notified"]):
            deals.append(Deal(m, rec["ref"], discount))
    return deals


def prune_state(state: dict[str, dict], today: date) -> None:
    for movie_id in [k for k, v in state.items() if age(v["seen"], today) > FORGET_AFTER_DAYS + SEEN_REFRESH_DAYS]:
        del state[movie_id]


# ---------- Telegram ----------

def format_caption(deal: Deal, prefix: str = "") -> str:
    m = deal.movie
    lines = [
        f"{prefix}🎬 <b>{html.escape(m.name)}</b>",
        f"💰 <s>NT${deal.ref_price}</s> → <b>NT${m.price}</b>（-{deal.discount:.0%}）",
    ]
    extra = [f"租借 NT${m.rental}"] if m.rental else []
    extra.append(html.escape(m.genre))
    lines.append("🏷 " + " · ".join(extra))
    lines.append(f'🔗 <a href="{html.escape(m.url)}">在 Apple TV 查看</a>')
    return "\n".join(lines)


def tg_call(session: requests.Session, token: str, method: str, payload: dict) -> tuple[bool, str]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    for _ in range(3):
        try:
            resp = session.post(url, data=payload, timeout=30)
            body = resp.json()
        except (requests.RequestException, ValueError) as e:
            return False, type(e).__name__  # 例外訊息含 token 網址，所以只回報類型
        if body.get("ok"):
            return True, ""
        if resp.status_code == 429:  # 發太快，依 Telegram 指示等待後重試
            time.sleep(body.get("parameters", {}).get("retry_after", 5) + 1)
            continue
        return False, body.get("description", f"HTTP {resp.status_code}")
    return False, "rate limited"


def send_deal(session: requests.Session, token: str, chat_id: str, deal: Deal, prefix: str = "") -> bool:
    caption = format_caption(deal, prefix)
    ok, err = tg_call(session, token, "sendPhoto",
                      {"chat_id": chat_id, "photo": deal.movie.image, "caption": caption, "parse_mode": "HTML"})
    if ok:
        return True
    log(f"  sendPhoto 失敗（{err}），改發純文字")
    ok, err = tg_call(session, token, "sendMessage", {"chat_id": chat_id, "text": caption, "parse_mode": "HTML"})
    if not ok:
        log(f"  sendMessage 失敗：{err}")
    return ok


# ---------- 主程式 ----------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="只列出特價，不發送也不寫入紀錄")
    ap.add_argument("--test", action="store_true", help="發一則範例推播，確認 Token 與 Chat ID 正確")
    args = ap.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not args.dry_run and not (token and chat_id):
        sys.exit("請設定環境變數 TELEGRAM_BOT_TOKEN 與 TELEGRAM_CHAT_ID（或加 --dry-run 只看結果）")

    session = requests.Session()
    session.headers["User-Agent"] = "apple-tv-deals/1.0"
    today = datetime.now(TAIPEI).date()
    movies = fetch_movies(session)

    if args.test:
        m = movies[0]
        sample = Deal(m, ref_price=m.price + 100, discount=100 / (m.price + 100))
        return 0 if send_deal(session, token, chat_id, sample, prefix="🧪 測試訊息（價格為假）\n") else 1

    state = load_state()
    first_run = not state
    deals = sorted(update_state(state, movies, today, MIN_DISCOUNT), key=lambda d: d.discount, reverse=True)
    prune_state(state, today)

    log(f"共 {len(movies)} 部電影，{len(deals)} 部符合特價條件（降幅 ≥ {MIN_DISCOUNT:.0%}）")
    if first_run:
        log("首次執行：已建立價格基準，之後有降價才會通知")

    if args.dry_run:
        for d in deals:
            log(f"  {d.movie.name}  NT${d.ref_price} → NT${d.movie.price}（-{d.discount:.0%}）")
        return 0

    failed = False
    for d in deals[:MAX_NOTIFY]:
        if not send_deal(session, token, chat_id, d):
            failed = True
            break  # 通常是 Token / Chat ID 有問題，不必一直重試
        state[d.movie.id]["notified"] = d.movie.price  # 成功才標記，失敗的明天會再試
        log(f"  已通知：{d.movie.name}")
        time.sleep(1.1)  # Telegram 對同一個聊天室限速約每秒 1 則
    if len(deals) > MAX_NOTIFY:
        log(f"  還有 {len(deals) - MAX_NOTIFY} 部超過單次上限 {MAX_NOTIFY}，留到下次推播")

    save_state(state)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
