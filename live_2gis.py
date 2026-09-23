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
INFO_TTL = 3600

_cache = {}  # firm_id -> {"at":..., "reviews":..., "meta":...}
_info_cache = {}  # firm_id -> {"at":..., "info":...}
_fetch_lock = None

DEFAULT_FIRM = FIRM_ID

def _lock():
    global _fetch_lock
    if _fetch_lock is None:
        import threading
        _fetch_lock = threading.Lock()
    return _fetch_lock


def _reviews_url(firm_id):
    return f"https://public-api.reviews.2gis.com/2.0/branches/{firm_id}/reviews"


def fetch_page_live(firm_id, limit=50, offset=0):
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
    r = requests.get(_reviews_url(firm_id), params=params, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_all_live(firm_id=None, force=False):
    """Возвращает (reviews, meta, from_cache). Кэш отдельный на каждое заведение."""
    firm_id = str(firm_id or DEFAULT_FIRM)
    entry = _cache.setdefault(firm_id, {"at": 0.0, "reviews": [], "meta": {}})
    now = time.time()
    if not force and entry["reviews"] and (now - entry["at"] < LIVE_CACHE_TTL):
        return entry["reviews"], entry["meta"], True
    with _lock():
        # пока ждали замок — кто-то уже мог обновить кэш
        now = time.time()
        if not force and entry["reviews"] and (now - entry["at"] < LIVE_CACHE_TTL):
            return entry["reviews"], entry["meta"], True
        if force and entry["reviews"] and (now - entry["at"] < 5):
            return entry["reviews"], entry["meta"], True
        all_r, meta = [], {}
        offset = 0
        while True:
            data = fetch_page_live(firm_id, limit=50, offset=offset)
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
        entry.update(at=time.time(), reviews=all_r, meta=meta)
        return all_r, meta, False


def refresh_in_background(firm_id=None):
    """Фоновое обновление кэша чтобы кнопка 'Обновить' отвечала мгновенно."""
    import threading

    def _job():
        try:
            fetch_all_live(firm_id, force=True)
        except Exception:
            pass
    t = threading.Thread(target=_job, daemon=True)
    t.start()
    return t


def clear_cache(firm_id=None):
    if firm_id:
        _cache.pop(str(firm_id), None)
    else:
        _cache.clear()


def resolve_share_url(url):
    """Ссылка 'Поделиться' из 2ГИС (короткая go.2gis.com или обычная) -> firm_id."""
    import re
    url = (url or "").strip()
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url
    m = re.search(r"/firm/(\d+)", url)
    if m and "go.2gis.com" not in url:
        return m.group(1)
    r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
    m = re.search(r"/firm/(\d+)", r.url)
    return m.group(1) if m else None


def branch_info(firm_id):
    """Название+адрес заведения со страницы отзывов 2ГИС (кэш 1 час)."""
    import re
    firm_id = str(firm_id)
    now = time.time()
    hit = _info_cache.get(firm_id)
    if hit and (now - hit["at"] < INFO_TTL):
        return hit["info"]
    if firm_id == str(DEFAULT_FIRM):
        info = {"firm_id": firm_id, "name": BRANCH_NAME, "address": BRANCH_ADDRESS,
                "url": BRANCH_URL, "short_url": SHORT_URL}
    else:
        info = {"firm_id": firm_id, "name": f"Заведение {firm_id}", "address": "",
                "url": f"https://2gis.kz/firm/{firm_id}/tab/reviews", "short_url": ""}
        try:
            r = requests.get(f"https://2gis.kz/firm/{firm_id}/tab/reviews",
                             headers=HEADERS, timeout=20, allow_redirects=True)
            m = re.search(r"<title>(.*?)</title>", r.text, re.S)
            title = (m.group(1).strip() if m else "")
            # "Отзывы о Генацвале, ресторан, мкр..., 11/1, Уральск - 2ГИС"
            core = re.sub(r"^Отзывы о\s+", "", title)
            core = re.sub(r"\s*[—-]\s*2ГИС\s*$", "", core).strip()
            parts = [p.strip() for p in core.split(",") if p.strip()]
            if len(parts) >= 3:
                info["name"] = ", ".join(parts[:2])
                info["address"] = ", ".join(parts[2:])
            elif len(parts) == 2:
                info["name"], info["address"] = parts
            elif parts:
                info["name"] = parts[0]
            info["url"] = r.url.split("?")[0]
        except Exception:
            pass
    _info_cache[firm_id] = {"at": now, "info": info}
    return info


def branch_live(meta, firm_id=None):
    meta = meta or {}
    info = branch_info(firm_id or DEFAULT_FIRM)
    entry = _cache.get(str(firm_id or DEFAULT_FIRM), {})
    return {
        "firm_id": info["firm_id"],
        "name": info["name"],
        "address": info["address"],
        "rating": meta.get("branch_rating") or 4.7,
        "reviews_count": meta.get("branch_reviews_count") or meta.get("total_count") or len(entry.get("reviews", [])),
        "ratings_count": 595 if str(info["firm_id"]) == str(DEFAULT_FIRM) else 0,
        "url": info["url"],
        "short_url": info.get("short_url") or "",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
