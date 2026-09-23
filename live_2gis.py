"""LIVE-режим: отзывы тянутся напрямую с 2ГИС при каждом показе (без чтения из SQLite).
SQLite остается только для черновиков ИИ. Кэш в памяти чтобы не долбить API на каждый клик.
"""
import json
import os
import time

import requests

from parser_2gis import (
    FIRM_ID, BRANCH_NAME, BRANCH_ADDRESS, BRANCH_URL, SHORT_URL,
    REVIEW_API_KEY, REVIEW_API_URL, HEADERS, normalize_review,
)

LIVE_CACHE_TTL = int(os.getenv("LIVE_CACHE_TTL", "60") or 60)

_cache = {"at": 0.0, "reviews": [], "meta": {}}
_fetch_lock = None

def _lock():
    global _fetch_lock
    if _fetch_lock is None:
        import threading
        _fetch_lock = threading.Lock()
    return _fetch_lock


def fetch_page_live(limit=50, offset=0):
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


def fetch_all_live(force=False):
    """Возвращает (reviews, meta, from_cache). reviews — список нормализованных отзывов.
    Параллельные запросы не долбят 2ГИС дважды: второй ждет замок и забирает свежий кэш."""
    now = time.time()
    if not force and _cache["reviews"] and (now - _cache["at"] < LIVE_CACHE_TTL):
        return _cache["reviews"], _cache["meta"], True
    with _lock():
        # пока ждали замок — кто-то уже мог обновить кэш
        now = time.time()
        if not force and _cache["reviews"] and (now - _cache["at"] < LIVE_CACHE_TTL):
            return _cache["reviews"], _cache["meta"], True
        if force and _cache["reviews"] and (now - _cache["at"] < 5):
            return _cache["reviews"], _cache["meta"], True
        all_r, meta = [], {}
        offset = 0
        while True:
            data = fetch_page_live(limit=50, offset=offset)
            meta = data.get("meta") or meta
            revs = data.get("reviews") or []
            if not revs:
                break
            for raw in revs:
                n = normalize_review(raw)
                try:
                    n["photos"] = json.loads(n.get("photos_json") or "[]")
                except Exception:
                    n["photos"] = []
                all_r.append(n)
            if len(revs) < 50:
                break
            offset += 50
            time.sleep(0.2)
        _cache.update(at=time.time(), reviews=all_r, meta=meta)
        return all_r, meta, False


def refresh_in_background():
    """Фоновое обновление кэша чтобы кнопка 'Обновить' отвечала мгновенно."""
    import threading

    def _job():
        try:
            fetch_all_live(force=True)
        except Exception:
            pass
    t = threading.Thread(target=_job, daemon=True)
    t.start()
    return t


def clear_cache():
    _cache["at"] = 0.0


def branch_live(meta):
    meta = meta or {}
    return {
        "firm_id": FIRM_ID,
        "name": BRANCH_NAME,
        "address": BRANCH_ADDRESS,
        "rating": meta.get("branch_rating") or 4.7,
        "reviews_count": meta.get("branch_reviews_count") or meta.get("total_count") or len(_cache["reviews"]),
        "ratings_count": 595,
        "url": BRANCH_URL,
        "short_url": SHORT_URL,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
