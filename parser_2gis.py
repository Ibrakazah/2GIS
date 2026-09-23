"""Парсер отзывов 2ГИС -> SQLite. Ресторан Генацвале, Уральск."""
import json
import sqlite3
import time
from datetime import datetime, timezone
import requests

DB_PATH = "reviews.db"

FIRM_ID = "70000001033566614"
SHORT_URL = "https://go.2gis.com/Ql70j"
BRANCH_URL = "https://2gis.kz/uralsk/firm/70000001033566614/tab/reviews"
BRANCH_NAME = "Генацвале, ресторан"
BRANCH_ADDRESS = "мкр. Кадыра Мырза Али, 11/1, Уральск"

REVIEW_API_KEY = "6e7e1929-4ea9-4a5d-8c05-d601860389bd"
REVIEW_API_URL = f"https://public-api.reviews.2gis.com/2.0/branches/{FIRM_ID}/reviews"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS branch (
        firm_id TEXT PRIMARY KEY,
        name TEXT,
        address TEXT,
        rating REAL,
        reviews_count INTEGER,
        ratings_count INTEGER,
        url TEXT,
        short_url TEXT,
        updated_at TEXT
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS reviews (
        id TEXT PRIMARY KEY,
        author TEXT,
        user_id TEXT,
        user_reviews_count INTEGER,
        rating INTEGER,
        date_created TEXT,
        date_edited TEXT,
        text TEXT,
        likes_count INTEGER,
        comments_count INTEGER,
        photos_json TEXT,
        official_answer TEXT,
        provider TEXT,
        is_verified INTEGER,
        region_id INTEGER
    )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_date ON reviews(date_created DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_rating ON reviews(rating)")
    conn.commit()
    conn.close()


def fetch_page(limit=50, offset=0):
    params = {
        "limit": limit,
        "offset": offset,
        "is_advertiser": "false",
        "fields": "meta.providers,meta.branch_rating,meta.branch_reviews_count,meta.total_count,reviews.hiding_reason,reviews.is_verified,reviews.emojis",
        "without_my_first_review": "false",
        "rated": "true",
        "sort_by": "date_edited",
        "key": REVIEW_API_KEY,
        "locale": "ru_RU",
    }
    r = requests.get(REVIEW_API_URL, params=params, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.json()


def normalize_review(r):
    user = r.get("user") or {}
    photos = []
    for p in (r.get("photos") or []):
        prev = (p.get("preview_urls") or {})
        photos.append(prev.get("640x") or prev.get("url") or "")
    oa = r.get("official_answer")
    if isinstance(oa, dict):
        oa_text = oa.get("text") or ""
        oa_date = oa.get("date_created") or ""
        official = f"{oa_text} ({oa_date})" if oa_text else ""
    elif isinstance(oa, str):
        official = oa
    else:
        official = ""
    return {
        "id": str(r.get("id")),
        "author": user.get("name") or "Аноним",
        "user_id": str(user.get("id") or ""),
        "user_reviews_count": int(user.get("reviews_count") or 0),
        "rating": int(r.get("rating") or 0),
        "date_created": r.get("date_created") or "",
        "date_edited": r.get("date_edited") or "",
        "text": (r.get("text") or "").strip(),
        "likes_count": int(r.get("likes_count") or 0),
        "comments_count": int(r.get("comments_count") or 0),
        "photos_json": json.dumps(photos, ensure_ascii=False),
        "official_answer": official,
        "provider": r.get("provider") or "2gis",
        "is_verified": 1 if r.get("is_verified") else 0,
        "region_id": int(r.get("region_id") or 0),
    }


def parse_all(progress_cb=None):
    """Качает ВСЕ отзывы с пагинацией, складывает в SQLite. Возвращает (всего, мета)."""
    init_db()
    offset = 0
    limit = 50
    total_saved = 0
    meta_info = {}
    conn = get_db()
    while True:
        data = fetch_page(limit=limit, offset=offset)
        meta = data.get("meta") or {}
        if not meta_info:
            meta_info = meta
        reviews = data.get("reviews") or []
        if not reviews:
            break
        for r in reviews:
            n = normalize_review(r)
            conn.execute("""
            INSERT OR REPLACE INTO reviews
            (id, author, user_id, user_reviews_count, rating, date_created, date_edited,
             text, likes_count, comments_count, photos_json, official_answer, provider, is_verified, region_id)
            VALUES (:id, :author, :user_id, :user_reviews_count, :rating, :date_created, :date_edited,
                    :text, :likes_count, :comments_count, :photos_json, :official_answer, :provider, :is_verified, :region_id)
            """, n)
            total_saved += 1
        conn.commit()
        if progress_cb:
            progress_cb(total_saved, meta.get("total_count", "?"))
        # конец если вернулось меньше лимита
        if len(reviews) < limit:
            break
        offset += limit
        time.sleep(0.25)  # вежливая пауза чтобы не забанили

    # сохраняем инфо о филиале
    rating = meta_info.get("branch_rating")
    reviews_count = meta_info.get("branch_reviews_count") or meta_info.get("total_count") or total_saved
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
    INSERT OR REPLACE INTO branch (firm_id, name, address, rating, reviews_count, ratings_count, url, short_url, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (FIRM_ID, BRANCH_NAME, BRANCH_ADDRESS, rating, reviews_count, 595, BRANCH_URL, SHORT_URL, now))
    conn.commit()
    conn.close()
    return total_saved, meta_info


if __name__ == "__main__":
    init_db()
    print(f"Парсим отзывы {BRANCH_NAME} ({FIRM_ID}) ...")

    def prog(saved, total):
        print(f"  сохранено: {saved}/{total}")

    saved, meta = parse_all(progress_cb=prog)
    print(f"Готово. Всего в базе: {saved}. Мета: {meta}")
