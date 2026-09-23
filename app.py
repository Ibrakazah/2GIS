"""Локальный сайт отзывов 2ГИС. Белый, список по дате публикации. Запуск: python app.py -> http://127.0.0.1:5000"""
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from flask import Flask, request, redirect, url_for, render_template, jsonify

from parser_2gis import init_db, parse_all, DB_PATH, FIRM_ID, BRANCH_NAME, BRANCH_ADDRESS, BRANCH_URL, SHORT_URL
import ai_helper
import live_2gis

app = Flask(__name__)

RU_MONTHS = {1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
             7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"}


def ru_date(value):
    """2026-09-23T17:01:58.843158+07:00 -> '23 сентября 2026, 17:01'."""
    if not value:
        return ""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return f"{dt.day} {RU_MONTHS[dt.month]} {dt.year}, {dt.hour:02d}:{dt.minute:02d}"
    except Exception:
        return str(value)[:16].replace("T", " ")


app.jinja_env.filters["ru_date"] = ru_date
init_db()  # SQLite теперь только для черновиков ИИ
ai_helper.ensure_ai_table(DB_PATH)
ai_helper.ensure_auto_tables(DB_PATH)

PER_PAGE = 20
CODE_V = 7  # версия кода — видна в подвале сайта, чтобы понимать что запущено
LIVE_MODE = os.getenv("LIVE_MODE", "1").strip() not in ("0", "false", "no")
AUTO_CHECK_MINUTES = int(os.getenv("AUTO_CHECK_MINUTES", "15") or 15)
_scheduler_started = False

# LIVE-память: какие отзывы уже видели (базовая линия = первый заход после старта).
# Базы отзывов больше нет — новые определяем сравнением с этим множеством.
_known_ids = set()
_baseline_at = ""
_auto_enabled = True
_last_check = ""
_last_gen = ""
_last_gen_count = 0


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_branch():
    conn = get_db()
    row = conn.execute("SELECT * FROM branch WHERE firm_id=?", (FIRM_ID,)).fetchone()
    conn.close()
    if row:
        return dict(row)
    return {"firm_id": FIRM_ID, "name": BRANCH_NAME, "address": BRANCH_ADDRESS,
            "rating": 4.7, "reviews_count": 0, "ratings_count": 595,
            "url": BRANCH_URL, "short_url": SHORT_URL, "updated_at": ""}


def detect_new_live(all_reviews):
    """Сравнение с памятью: первый заход = базовая линия, дальше — новые. Возвращает список новых."""
    global _baseline_at
    global _last_check
    _last_check = datetime.now(timezone.utc).isoformat()
    if not _known_ids:
        for r in all_reviews:
            _known_ids.add(str(r["id"]))
        _baseline_at = _last_check
        return []
    fresh = [r for r in all_reviews if str(r["id"]) not in _known_ids]
    for r in fresh:
        _known_ids.add(str(r["id"]))
    return sorted(fresh, key=lambda x: x.get("date_created") or "", reverse=True)


def get_auto_status():
    try:
        conn = get_db()
        drafts = conn.execute("SELECT COUNT(*) c FROM ai_drafts").fetchone()["c"]
        conn.close()
    except Exception:
        drafts = 0
    return {"enabled": _auto_enabled, "known": len(_known_ids), "pending": 0, "drafts": drafts,
            "baseline_at": _baseline_at, "last_check": _last_check,
            "last_auto_gen": _last_gen, "last_auto_count": str(_last_gen_count),
            "interval_min": AUTO_CHECK_MINUTES, "model": ai_helper.AI_MODEL,
            "live": LIVE_MODE}


def auto_check_cycle():
    """Одна итерация фона: свежие отзывы с 2ГИС + автогенерация (по 3 варианта) для новых."""
    global _last_gen, _last_gen_count
    try:
        all_r, meta, _ = live_2gis.fetch_all_live(force=True)
    except Exception as e:
        return {"ok": False, "error": f"live fetch: {e}"}
    fresh = detect_new_live(all_r)
    if not fresh or not _auto_enabled:
        return {"ok": True, "parsed": len(all_r), "new_drafts": 0, "details": []}
    gen = []
    for r in fresh[:ai_helper.AUTO_MAX_PER_RUN]:
        try:
            variants, source = ai_helper.generate_ai_variants(
                r.get("author") or "Гость", r.get("rating") or 0, r.get("text") or "", BRANCH_NAME)
            ai_helper.save_draft(DB_PATH, str(r["id"]), ai_helper.pack_variants(variants), source)
            gen.append({"review_id": str(r["id"]), "source": source})
        except Exception as e:
            gen.append({"review_id": str(r["id"]), "source": f"error: {e}"})
        time.sleep(2.0)
    _last_gen = datetime.now(timezone.utc).isoformat()
    _last_gen_count = len(gen)
    return {"ok": True, "parsed": len(all_r), "new_drafts": len(gen), "details": gen}


def scheduler_loop():
    while True:
        try:
            if _auto_enabled:
                auto_check_cycle()
        except Exception:
            pass
        time.sleep(max(60, AUTO_CHECK_MINUTES * 60))


def start_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()


@app.before_request
def _ensure_scheduler():
    # для gunicorn/PaaS: планировщик стартует при первом запросе (в dev — тоже)
    start_scheduler()


@app.route("/")
def index():
    q = (request.args.get("q") or "").strip().lower()
    rating = (request.args.get("rating") or "all").strip()
    sort = (request.args.get("sort") or "new").strip()  # new = сначала новые (по дате публикации DESC)
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    updated = request.args.get("updated")
    live_cached = True

    if LIVE_MODE:
        # ЖИВОЙ режим: отзывы напрямую с 2ГИС, без базы. Кэш 60 сек.
        try:
            all_r, meta, live_cached = live_2gis.fetch_all_live()
        except Exception as e:
            # если 2ГИС недоступен — показываем что есть в старой SQLite-копии
            conn = get_db()
            rows = conn.execute("SELECT * FROM reviews ORDER BY date_created DESC LIMIT 1000").fetchall()
            conn.close()
            all_r = []
            for r in rows:
                d = dict(r)
                try:
                    d["photos"] = json.loads(d.get("photos_json") or "[]")
                except Exception:
                    d["photos"] = []
                all_r.append(d)
            meta = {}
            updated = updated or f"2ГИС недоступен ({e}), показан кэш из базы"
        fresh = detect_new_live(all_r)
        if fresh and _auto_enabled:
            def _bg(items):
                global _last_gen, _last_gen_count
                gen = []
                for r in items[:ai_helper.AUTO_MAX_PER_RUN]:
                    try:
                        variants, source = ai_helper.generate_ai_variants(
                            r.get("author") or "Гость", r.get("rating") or 0,
                            r.get("text") or "", BRANCH_NAME)
                        ai_helper.save_draft(DB_PATH, str(r["id"]), ai_helper.pack_variants(variants), source)
                        gen.append(source)
                    except Exception:
                        pass
                    time.sleep(2.0)
                if gen:
                    _last_gen = datetime.now(timezone.utc).isoformat()
                    _last_gen_count = len(gen)
            threading.Thread(target=_bg, args=(fresh,), daemon=True).start()
        branch = live_2gis.branch_live(meta)
        # фильтр + сортировка по дате публикации в памяти
        filt = all_r
        if q:
            filt = [r for r in filt if q in (r.get("author") or "").lower() or q in (r.get("text") or "").lower()]
        if rating in ("1", "2", "3", "4", "5"):
            filt = [r for r in filt if int(r.get("rating") or 0) == int(rating)]
        filt = sorted(filt, key=lambda x: x.get("date_created") or "", reverse=(sort != "old"))
        total = len(filt)
        pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page = min(page, pages)
        reviews = filt[(page - 1) * PER_PAGE:page * PER_PAGE]
        dist = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        s = 0
        for r in all_r:
            try:
                rt = int(r.get("rating") or 0)
            except Exception:
                rt = 0
            if rt in dist:
                dist[rt] += 1
                s += rt
        avg = round(s / len(all_r), 2) if all_r else 0
        db_count = len(all_r)
    else:
        where = []
        params = []
        if q:
            where.append("(author LIKE ? OR text LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if rating in ("1", "2", "3", "4", "5"):
            where.append("rating = ?")
            params.append(int(rating))
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        order_sql = "ORDER BY date_created DESC" if sort != "old" else "ORDER BY date_created ASC"
        conn = get_db()
        total = conn.execute(f"SELECT COUNT(*) c FROM reviews {where_sql}", params).fetchone()["c"]
        pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page = min(page, pages)
        offset = (page - 1) * PER_PAGE
        rows = conn.execute(
            f"SELECT * FROM reviews {where_sql} {order_sql} LIMIT ? OFFSET ?",
            params + [PER_PAGE, offset]).fetchall()
        stats = conn.execute("SELECT rating, COUNT(*) c FROM reviews GROUP BY rating").fetchall()
        avg_row = conn.execute("SELECT AVG(rating) a, COUNT(*) c FROM reviews").fetchone()
        conn.close()
        dist = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        for st in stats:
            dist[int(st["rating"] or 0)] = st["c"]
        avg = round(avg_row["a"] or 0, 2)
        db_count = avg_row["c"]
        reviews = []
        for r in rows:
            d = dict(r)
            try:
                d["photos"] = json.loads(d.get("photos_json") or "[]")
            except Exception:
                d["photos"] = []
            reviews.append(d)
        branch = get_branch()

    # подтягиваем сохраненные черновики ИИ (по 3 варианта)
    try:
        drafts = ai_helper.get_drafts_map(DB_PATH, [r["id"] for r in reviews])
        for d in reviews:
            dr = drafts.get(str(d["id"]))
            if dr:
                d["ai_variants"] = ai_helper.unpack_variants(dr["draft_text"])
                d["ai_source"] = dr["source"]
                joined = " ".join(d["ai_variants"])
                d["ai_old"] = not ("С уважением" in joined or "Уважаемый" in joined)
            else:
                d["ai_variants"] = []
                d["ai_source"] = ""
                d["ai_old"] = False
    except Exception:
        for d in reviews:
            d["ai_variants"] = []
            d["ai_source"] = ""
            d["ai_old"] = False

    ai_enabled = bool(ai_helper.AI_API_KEY)
    auto = get_auto_status()
    return render_template("index.html",
                           branch=branch, reviews=reviews,
                           q=request.args.get("q") or "", rating=rating, sort=sort,
                           page=page, pages=pages, total=total,
                           dist=dist, avg=avg,
                           db_count=db_count, updated=updated, per_page=PER_PAGE,
                           ai_enabled=ai_enabled, ai_model=ai_helper.AI_MODEL,
                           auto=auto, live_mode=LIVE_MODE, live_cached=live_cached,
                           code_v=CODE_V)


@app.route("/parse", methods=["POST"])
def do_parse():
    if LIVE_MODE:
        # мгновенный ответ: обновление идет в фоне, страница сразу из теплого кэша
        live_2gis.refresh_in_background()
        return redirect(url_for("index", updated="bg"))
    saved, meta = parse_all()
    def _bg():
        try:
            branch = get_branch()
            ai_helper.auto_generate_new(DB_PATH, branch.get("name") or BRANCH_NAME, sleep_s=1.5)
        except Exception:
            pass
    threading.Thread(target=_bg, daemon=True).start()
    return redirect(url_for("index", updated=saved))


@app.route("/auto-toggle", methods=["POST"])
def auto_toggle():
    global _auto_enabled, _baseline_at
    _auto_enabled = not _auto_enabled
    if _auto_enabled and not _known_ids:
        # при включении с пустой памятью — следующий заход станет базовой линией
        _baseline_at = ""
    return redirect(url_for("index"))


@app.route("/api/auto-status")
def api_auto_status():
    return jsonify(get_auto_status())


@app.route("/ai-reply/<review_id>", methods=["POST"])
def ai_reply(review_id):
    """Кнопка 'Сгенерировать' -> JSON {variants:[v1,v2,v3], source}. Только черновик, НЕ публикуется в 2ГИС.
    Тело запроса (опционально): {"previous": [...], "regen": true} — для 'Перегенерировать':
    модель получит инструкцию выдать ДРУГИЕ формулировки + температура 1.0."""
    rid = str(review_id)
    try:
        body = request.get_json(force=False, silent=True) or {}
    except Exception:
        body = {}
    previous = body.get("previous") if isinstance(body.get("previous"), list) else []
    regen = bool(body.get("regen"))
    found = None
    try:
        all_r, _, _ = live_2gis.fetch_all_live()
        for r in all_r:
            if str(r["id"]) == rid:
                found = r
                break
    except Exception:
        pass
    if not found:  # запасной вариант — старая SQLite-копия
        conn = get_db()
        row = conn.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone()
        conn.close()
        if row:
            found = dict(row)
    if not found:
        return jsonify({"ok": False, "error": "review not found"}), 404
    variants, source = ai_helper.generate_ai_variants(
        found.get("author") or "Гость", found.get("rating") or 0, found.get("text") or "", BRANCH_NAME,
        previous=previous[:3], temperature=1.0 if regen else 0.7)
    ai_helper.save_draft(DB_PATH, rid, ai_helper.pack_variants(variants), source)
    return jsonify({"ok": True, "review_id": rid, "variants": variants, "source": source})


@app.route("/api/drafts")
def api_drafts():
    conn = get_db()
    rows = conn.execute("SELECT * FROM ai_drafts ORDER BY created_at DESC LIMIT 500").fetchall()
    conn.close()
    return jsonify([dict(x) for x in rows])


@app.route("/drafts/clear", methods=["POST"])
def drafts_clear():
    """Удалить все сохраненные черновики (например старые, обрезанные или неформальные)."""
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) c FROM ai_drafts").fetchone()["c"]
    conn.execute("DELETE FROM ai_drafts")
    conn.commit()
    conn.close()
    return redirect(url_for("index", updated=f"очищено черновиков: {n}"))


@app.route("/api/reviews")
def api_reviews():
    try:
        all_r, meta, cached = live_2gis.fetch_all_live()
        return jsonify(all_r[:1000])
    except Exception as e:
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    import webbrowser

    def _already_running(port):
        """Проверка: наш ли сервер уже висит на порту (защита от двойного запуска)."""
        try:
            import requests as _rq
            r = _rq.get(f"http://127.0.0.1:{port}/api/auto-status", timeout=2)
            j = r.json()
            return isinstance(j, dict) and "live" in j
        except Exception:
            return False

    host = os.getenv("HOST", "127.0.0.1")
    want_port = int(os.getenv("PORT", "5000") or 5000)
    if _already_running(want_port):
        print("=" * 60)
        print(f"Сервер УЖЕ запущен на http://localhost:{want_port}")
        print("Это окно можно закрыть. Открывай сайт в браузере.")
        print("Если хочешь перезапустить — закрой СТАРОЕ черное окно сервера и запусти снова.")
        print("=" * 60)
        raise SystemExit(0)
    for port in range(want_port, want_port + 11):
        try:
            print("=" * 60)
            print("Сервер запускается... НЕ ЗАКРЫВАЙ это окно пока смотришь сайт.")
            print(f"Открывай в браузере: http://localhost:{port}")
            print(f"(то же самое: http://127.0.0.1:{port})")
            print("Остановка сервера: Ctrl+C в этом окне.")
            print("=" * 60)
            if host in ("127.0.0.1", "localhost") and os.getenv("NO_BROWSER") != "1":
                threading.Timer(1.5, lambda p=port: webbrowser.open(f"http://localhost:{p}")).start()
            start_scheduler()
            app.run(host=host, port=port, debug=False)
            bound = port
            break
        except OSError as e:
            print(f"Порт {port} занят, пробую следующий... ({e})")
    if bound is None and 'port' in dir():
        print("Не нашлось свободного порта. Закрой программу занявшую 5000-е порты и попробуй снова.")
