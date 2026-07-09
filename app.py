"""
台股注意股・處置股查詢系統 — Flask Web 伺服器
支援上市（TWSE）+ 上櫃（TPEx）股票
本機開發：python app.py
雲端部署：上傳至 Render / Railway，啟動指令 gunicorn app:app
"""

import os
import re
import hashlib
from dotenv import load_dotenv
load_dotenv()
import sqlite3
import requests
from collections import defaultdict
from datetime import datetime, timedelta
from flask import Flask, make_response, render_template, request, Response, jsonify
from cloud_db import open_db, using_turso

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder='static', template_folder='templates')

def _file_hash(rel_path):
    """回傳靜態檔案內容的 MD5 前8碼，供 cache busting 使用"""
    try:
        with open(os.path.join(BASE_DIR, rel_path), 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()[:8]
    except Exception:
        return '0'

# ── SQLite 快取資料庫 ─────────────────────────────────
DB_PATH = os.path.join(BASE_DIR, 'cache.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS tpex_stock_list (
            code    TEXT PRIMARY KEY,
            name    TEXT,
            updated TEXT
        );
    ''')
    conn.commit()
    conn.close()

init_db()

# ── HTTP Headers ──────────────────────────────────────
TWSE_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Referer': 'https://www.twse.com.tw/',
    'Accept':  'application/json, text/plain, */*',
}
TPEX_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Referer': 'https://www.tpex.org.tw/',
    'Accept':  'application/json, text/plain, */*',
}

# ── TWSE API 對應表 ──────────────────────────────────
TWSE_MAP = {
    'stock_day': 'https://www.twse.com.tw/exchangeReport/STOCK_DAY',
    'notice':    'https://www.twse.com.tw/announcement/notice',
    'punish':    'https://www.twse.com.tw/announcement/punish',
    'stocklist': 'https://www.twse.com.tw/exchangeReport/BWIBBU_d',
}

# ── TPEx OpenAPI ─────────────────────────────────────
TPEX_BASE = 'https://www.tpex.org.tw/openapi/v1'
TPEX_MAP  = {
    'notice':   f'{TPEX_BASE}/tpex_trading_warning_information',
    'disposal': f'{TPEX_BASE}/tpex_disposal_information',
    'stocks':   f'{TPEX_BASE}/tpex_mainboard_daily_close_quotes',
}


# ── 全域例外攔截器：確保任何錯誤都回傳 JSON，不吐 HTML ──
@app.errorhandler(Exception)
def handle_exception(e):
    import traceback
    traceback.print_exc()
    return jsonify(stat='ERROR', message=f'伺服器內部錯誤：{str(e)}'), 500

@app.errorhandler(404)
def handle_404(e):
    return jsonify(stat='ERROR', message='路徑不存在'), 404


# ── 靜態頁面 ─────────────────────────────────────────
@app.route('/')
def index():
    resp = make_response(render_template(
        'index.html',
        css_v=_file_hash('static/css/style.css'),
        js_v =_file_hash('static/js/main.js'),
    ))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma']  = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route('/ads.txt')
def ads_txt():
    return app.send_static_file('ads.txt')


@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/guide')
def guide():
    return render_template('guide.html')


@app.route('/faq')
def faq():
    return render_template('faq.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


# ── TWSE 代理 ─────────────────────────────────────────
@app.route('/proxy/<name>')
def twse_proxy(name):
    if name not in TWSE_MAP:
        return jsonify(stat='ERROR', message=f'未知端點：{name}'), 404
    url    = TWSE_MAP[name]
    params = dict(request.args)
    try:
        resp = requests.get(url, params=params, headers=TWSE_HEADERS, timeout=15)
        resp.raise_for_status()
        # 驗證回傳確實是 JSON（TWSE 有時對海外 IP 回傳 HTML 攔截頁）
        try:
            resp.json()
        except ValueError:
            preview = resp.text[:200].replace('\n', ' ')
            return jsonify(stat='ERROR',
                           message=f'TWSE 回傳非 JSON 格式（可能封鎖海外 IP）。回應前200字：{preview}'), 502
        return Response(
            resp.content, status=200,
            content_type='application/json;charset=UTF-8',
            headers={'Access-Control-Allow-Origin': '*'}
        )
    except requests.exceptions.Timeout:
        return jsonify(stat='ERROR', message='TWSE 回應逾時，請稍後再試'), 504
    except requests.exceptions.RequestException as e:
        return jsonify(stat='ERROR', message=str(e)), 502


# ── TPEx 代理 ─────────────────────────────────────────
@app.route('/tpex/<name>')
def tpex_proxy(name):
    if name not in TPEX_MAP:
        return jsonify(stat='ERROR', message=f'未知端點：{name}'), 404
    url    = TPEX_MAP[name]
    params = dict(request.args)
    try:
        resp = requests.get(url, params=params, headers=TPEX_HEADERS, timeout=20)
        resp.raise_for_status()
        # 驗證回傳確實是 JSON
        try:
            resp.json()
        except ValueError:
            preview = resp.text[:200].replace('\n', ' ')
            return jsonify(stat='ERROR',
                           message=f'TPEx 回傳非 JSON 格式。回應前200字：{preview}'), 502
        return Response(
            resp.content, status=200,
            content_type='application/json;charset=UTF-8',
            headers={'Access-Control-Allow-Origin': '*'}
        )
    except requests.exceptions.Timeout:
        return jsonify(stat='ERROR', message='TPEx 回應逾時，請稍後再試'), 504
    except requests.exceptions.RequestException as e:
        return jsonify(stat='ERROR', message=str(e)), 502




# ── TPEx 上櫃股票清單（快取 6 小時）─────────────────────
@app.route('/tpex/stocklist')
def tpex_stocklist():
    conn = get_db()
    rows = conn.execute(
        'SELECT code, name, updated FROM tpex_stock_list ORDER BY code'
    ).fetchall()

    need_refresh = not rows
    if rows:
        try:
            last_updated = datetime.fromisoformat(rows[0]['updated'])
            if (datetime.utcnow() - last_updated).total_seconds() > 21600:
                need_refresh = True
        except Exception:
            need_refresh = True

    if need_refresh:
        try:
            resp = requests.get(TPEX_MAP['stocks'], headers=TPEX_HEADERS, timeout=20)
            resp.raise_for_status()
            data    = resp.json()
            now_str = datetime.utcnow().isoformat()
            to_ins  = [
                (item['SecuritiesCompanyCode'].strip(),
                 item['CompanyName'].strip(),
                 now_str)
                for item in data
                if item.get('SecuritiesCompanyCode') and item.get('CompanyName')
            ]
            if to_ins:
                conn.executemany(
                    'INSERT OR REPLACE INTO tpex_stock_list (code,name,updated) VALUES (?,?,?)',
                    to_ins
                )
                conn.commit()
            rows = conn.execute(
                'SELECT code, name, updated FROM tpex_stock_list ORDER BY code'
            ).fetchall()
        except Exception as e:
            print(f'[TPEx stocklist] error: {e}')

    conn.close()
    out  = [{'code': r['code'], 'name': r['name']} for r in rows]
    resp = jsonify(out)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


# ══════════════════════════════════════════════════════
# ── 股市資料庫 API（stock_data.db）─────────────────────
# ══════════════════════════════════════════════════════

STOCK_DB_PATH = os.path.join(BASE_DIR, 'stock_data.db')

def get_stock_db():
    # Turso cloud DB 優先；否則用本機 stock_data.db
    if using_turso():
        return open_db(STOCK_DB_PATH)
    if not os.path.exists(STOCK_DB_PATH):
        return None
    return open_db(STOCK_DB_PATH)


@app.route('/db/stocklist')
def db_stocklist():
    """從 stock_data.db 取得股票清單（供自動完成，取代 TWSE/TPEx 直連）"""
    market = request.args.get('market', '').upper()
    conn = get_stock_db()
    if not conn:
        return jsonify([])
    try:
        # 從 stocks 表取有名稱的股票
        if market:
            named = conn.execute(
                'SELECT code, name, market FROM stocks WHERE market=? ORDER BY code',
                (market,)
            ).fetchall()
        else:
            named = conn.execute(
                'SELECT code, name, market FROM stocks ORDER BY code'
            ).fetchall()
        named_set = {(r['code'], r['market']) for r in named}

        # 從 daily_price 補上沒有名稱的（近 7 天有資料的）
        if market:
            priced = conn.execute(
                '''SELECT DISTINCT code, market FROM daily_price
                   WHERE market=? AND date >= date('now','-7 days') ORDER BY code''',
                (market,)
            ).fetchall()
        else:
            priced = conn.execute(
                '''SELECT DISTINCT code, market FROM daily_price
                   WHERE date >= date('now','-7 days') ORDER BY code'''
            ).fetchall()

        out = [{'code': r['code'], 'name': r['name'], 'market': r['market']}
               for r in named]
        for r in priced:
            if (r['code'], r['market']) not in named_set:
                out.append({'code': r['code'], 'name': '', 'market': r['market']})

        resp = jsonify(out)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Cache-Control'] = 'public, max-age=900, stale-while-revalidate=3600'
        return resp
    finally:
        conn.close()


@app.route('/db/status')
def db_status():
    """回傳 stock_data.db 最後更新狀態"""
    conn = get_stock_db()
    if not conn:
        return jsonify({'ready': False, 'message': '資料庫尚未建立，請先執行 stock_db.py'}), 404
    try:
        log = conn.execute(
            'SELECT updated_at, status, message FROM update_log ORDER BY id DESC LIMIT 1'
        ).fetchone()
        price_range = conn.execute(
            'SELECT MIN(date) AS oldest, MAX(date) AS newest, COUNT(*) AS total FROM daily_price'
        ).fetchone()
        notice_cnt = conn.execute('SELECT COUNT(*) FROM notice_stocks').fetchone()[0]
        disp_cnt   = conn.execute('SELECT COUNT(*) FROM disposition_stocks').fetchone()[0]
        return jsonify({
            'ready':        True,
            'last_updated': log['updated_at'] if log else None,
            'status':       log['status']     if log else None,
            'summary':      log['message']    if log else None,
            'price_oldest': price_range['oldest'],
            'price_newest': price_range['newest'],
            'price_total':  price_range['total'],
            'notice_total': notice_cnt,
            'disp_total':   disp_cnt,
        })
    finally:
        conn.close()


@app.route('/db/prices')
def db_prices():
    """
    從 stock_data.db 取得指定股票的近60日收盤資料
    ?code=2330&market=TSE
    """
    code   = request.args.get('code', '').strip()
    market = request.args.get('market', 'TSE').upper()
    if not code:
        return jsonify({'error': 'code required'}), 400
    conn = get_stock_db()
    if not conn:
        return jsonify({'error': '資料庫尚未建立'}), 404
    try:
        rows = conn.execute(
            '''SELECT date, open, high, low, close, volume, change, pct
               FROM daily_price
               WHERE code=? AND market=?
               ORDER BY date
               LIMIT 90''',
            (code, market)
        ).fetchall()
        if not rows:
            return jsonify({'error': f'DB 查無 {code}（{market}）股價資料'}), 404
        out = [dict(r) for r in rows]
        resp = jsonify(out)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    finally:
        conn.close()


@app.route('/db/notices')
def db_notices():
    """
    從 stock_data.db 取得指定股票的注意股歷史
    ?code=2330&market=TSE&days=30
    """
    code   = request.args.get('code', '').strip()
    market = request.args.get('market', 'TSE').upper()
    days   = int(request.args.get('days', 30))
    if not code:
        return jsonify({'error': 'code required'}), 400
    conn = get_stock_db()
    if not conn:
        return jsonify({'error': '資料庫尚未建立'}), 404
    try:
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d')
        rows = conn.execute(
            '''SELECT date, name, reason, close
               FROM notice_stocks
               WHERE code=? AND market=? AND date >= ?
               ORDER BY date DESC''',
            (code, market, cutoff)
        ).fetchall()
        out = [dict(r) for r in rows]
        resp = jsonify(out)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    finally:
        conn.close()


@app.route('/db/disposals')
def db_disposals():
    """
    從 stock_data.db 取得指定股票的現行處置股記錄
    ?code=2330&market=TSE
    """
    code   = request.args.get('code', '').strip()
    market = request.args.get('market', 'TSE').upper()
    if not code:
        return jsonify({'error': 'code required'}), 400
    conn = get_stock_db()
    if not conn:
        return jsonify({'error': '資料庫尚未建立'}), 404
    try:
        rows = conn.execute(
            '''SELECT announce_date, start_date, end_date, reason, measure, content
               FROM disposition_stocks
               WHERE code=? AND market=?
               ORDER BY announce_date DESC''',
            (code, market)
        ).fetchall()
        out = [dict(r) for r in rows]
        resp = jsonify(out)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    finally:
        conn.close()


@app.route('/db/all_notices')
def db_all_notices():
    """
    回傳 DB 中所有注意股/處置股代碼清單（供前端判斷哪些股票在名單中）
    ?market=TSE&days=30
    """
    market = request.args.get('market', '').upper()
    days   = int(request.args.get('days', 30))
    conn = get_stock_db()
    if not conn:
        return jsonify({'notices': [], 'disposals': [], 'upcoming': []}), 200
    try:
        today  = datetime.now().strftime('%Y-%m-%d')
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

        notices = conn.execute(
            "SELECT DISTINCT code, name, market FROM notice_stocks "
            "WHERE date >= ? AND (market=? OR ?='') ORDER BY code",
            (cutoff, market, market)
        ).fetchall()

        # 處置中：已開始（start_date <= today 或 start_date 為空），只取4碼股票
        # 含今日最新漲跌額（LEFT JOIN daily_price 最新一天）
        disposals = conn.execute(
            """
            SELECT d.code, d.name, d.market,
                   COALESCE(p.change, 0) AS price_change,
                   COALESCE(p.close,  0) AS close_price
            FROM (
                SELECT DISTINCT code, name, market
                FROM disposition_stocks
                WHERE (start_date = '' OR start_date IS NULL OR start_date <= ?)
                  AND (market=? OR ?='')
                  AND LENGTH(code) = 4
            ) d
            LEFT JOIN daily_price p
              ON p.code=d.code AND p.market=d.market
             AND p.date=(
                 SELECT MAX(date) FROM daily_price p2
                 WHERE p2.code=d.code AND p2.market=d.market
             )
            ORDER BY CAST(d.code AS INTEGER)
            """,
            (today, market, market)
        ).fetchall()

        # 即將處置：已公告但尚未開始（start_date > today），只取4碼股票
        upcoming = conn.execute(
            """
            SELECT DISTINCT code, name, market, start_date, announce_date
            FROM disposition_stocks
            WHERE start_date > ? AND start_date != ''
              AND (market=? OR ?='')
              AND LENGTH(code) = 4
            ORDER BY start_date, CAST(code AS INTEGER)
            """,
            (today, market, market)
        ).fetchall()

        resp = jsonify({
            'notices':   [dict(r) for r in notices],
            'disposals': [dict(r) for r in disposals],
            'upcoming':  [dict(r) for r in upcoming],
        })
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    finally:
        conn.close()


@app.route('/db/exiting_disposal')
def db_exiting_disposal():
    """
    回傳今天是最後一天處置的股票（end_date = today → 明天解除）
    """
    conn = get_stock_db()
    if not conn:
        return jsonify([]), 200
    try:
        today_str = datetime.now().strftime('%Y-%m-%d')
        rows = conn.execute(
            """
            SELECT d.code, d.name, d.market, d.end_date,
                   COALESCE(p.change, 0) AS price_change,
                   COALESCE(p.close,  0) AS close_price
            FROM (
                SELECT DISTINCT code, name, market, end_date
                FROM disposition_stocks
                WHERE end_date = ? AND LENGTH(code) = 4
                  AND NOT EXISTS (
                      SELECT 1 FROM disposition_stocks d2
                      WHERE d2.code   = disposition_stocks.code
                        AND d2.market = disposition_stocks.market
                        AND d2.end_date > ?
                  )
            ) d
            LEFT JOIN daily_price p
              ON p.code=d.code AND p.market=d.market
             AND p.date=(
                 SELECT MAX(date) FROM daily_price p2
                 WHERE p2.code=d.code AND p2.market=d.market
             )
            ORDER BY CAST(d.code AS INTEGER)
            """,
            (today_str, today_str)
        ).fetchall()
        resp = jsonify([dict(r) for r in rows])
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    finally:
        conn.close()


@app.route('/db/approaching_disposal')
def db_approaching_disposal():
    """
    計算所有注意股的四條處置路線進度，回傳「再一次就觸發處置」的股票
    """
    conn = get_stock_db()
    if not conn:
        return jsonify([]), 200
    try:
        today     = datetime.now().date()
        cut42     = (today - timedelta(days=42)).strftime('%Y-%m-%d')
        cut14     = (today - timedelta(days=14)).strftime('%Y-%m-%d')
        today_str = today.strftime('%Y-%m-%d')

        # 目前處置中的股票（排除用）
        disposed_set = {
            (r['code'], r['market'])
            for r in conn.execute(
                "SELECT DISTINCT code, market FROM disposition_stocks "
                "WHERE start_date <= ? AND (end_date = '' OR end_date IS NULL OR end_date >= ?)",
                (today_str, today_str)
            ).fetchall()
        }

        # 近42天注意紀錄（只取4碼股票，排除已處置）
        rows = conn.execute(
            "SELECT code, name, market, date, reason FROM notice_stocks "
            "WHERE date >= ? AND LENGTH(code)=4 ORDER BY code, market, date DESC",
            (cut42,)
        ).fetchall()

        # 交易日集合（用於判斷真正連續）
        tdays = conn.execute(
            "SELECT DISTINCT date FROM daily_price WHERE date >= ?", (cut42,)
        ).fetchall()
        tday_set = {r['date'] for r in tdays}

        # 最新一天各股漲跌
        price_rows = conn.execute(
            "SELECT code, market, change AS price_change, close AS close_price "
            "FROM daily_price "
            "WHERE date=(SELECT MAX(date) FROM daily_price) AND LENGTH(code)=4"
        ).fetchall()
        price_map = {
            (r['code'], r['market']): {'change': r['price_change'], 'close': r['close_price']}
            for r in price_rows
        }

        # 依股票分組
        stock_notices = defaultdict(list)
        stock_names   = {}
        for r in rows:
            key = (r['code'], r['market'])
            stock_notices[key].append({'date': r['date'], 'reason': r['reason'] or ''})
            stock_names.setdefault(key, r['name'])

        def is_consec(d1, d2):
            """d1 > d2，中間沒有交易日 = 連續交易日"""
            from datetime import date as _date, timedelta as _td
            cur = _date.fromisoformat(d2) + _td(days=1)
            end = _date.fromisoformat(d1)
            while cur < end:
                if cur.strftime('%Y-%m-%d') in tday_set:
                    return False
                cur += _td(days=1)
            return True

        def streak_live(streak_dates):
            """最後一次注意後沒有缺席的交易日，streak 仍有效"""
            if not streak_dates:
                return False
            from datetime import date as _date, timedelta as _td
            last = _date.fromisoformat(streak_dates[0])
            dset = set(streak_dates)
            cur  = last + _td(days=1)
            while cur <= today:
                s = cur.strftime('%Y-%m-%d')
                if s in tday_set and s not in dset:
                    return False
                cur += _td(days=1)
            return True

        def calc_streak(dates):
            cnt = 0
            for i, d in enumerate(dates):
                if i == 0:
                    cnt = 1; continue
                if is_consec(dates[i-1], d):
                    cnt += 1
                else:
                    break
            return cnt

        result = []
        for (code, market), notices in stock_notices.items():
            if (code, market) in disposed_set:
                continue
            notices.sort(key=lambda x: x['date'], reverse=True)
            dates  = [n['date'] for n in notices]
            dates1 = [n['date'] for n in notices if '第一款' in n['reason']]

            cnt10 = sum(1 for d in dates if d >= cut14)
            cnt30 = len(dates)

            s2 = calc_streak(dates)
            s1 = calc_streak(dates1)
            live2 = s2 if streak_live(dates[:s2])  else 0
            live1 = s1 if streak_live(dates1[:s1]) else 0

            r1need = max(0, 3 - live1)
            r2need = max(0, 5 - live2)
            r3need = max(0, 6 - cnt10)
            r4need = max(0, 12 - cnt30)
            min_need = min(r1need, r2need, r3need, r4need)

            if min_need == 1:
                pm  = price_map.get((code, market), {'change': 0, 'close': 0})
                result.append({
                    'code':         code,
                    'name':         stock_names[(code, market)],
                    'market':       market,
                    'price_change': pm['change'],
                    'close_price':  pm['close'],
                    'r1need': r1need, 'r2need': r2need,
                    'r3need': r3need, 'r4need': r4need,
                    'min_need': min_need,
                })

        result.sort(key=lambda x: int(x['code']))
        resp = jsonify(result)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    finally:
        conn.close()


_comments_migrated = False

def ensure_comments_schema(conn):
    """舊 DB 補 comments.code / market 欄位（每個 worker 只跑一次）"""
    global _comments_migrated
    if _comments_migrated:
        return
    for _sql in ('ALTER TABLE comments ADD COLUMN code TEXT',
                 'ALTER TABLE comments ADD COLUMN market TEXT',
                 'ALTER TABLE comments ADD COLUMN color TEXT'):
        try:
            conn.execute(_sql)
        except Exception:
            pass
    _comments_migrated = True


@app.route('/api/comments', methods=['GET'])
def get_comments():
    code   = (request.args.get('code') or '').strip()
    market = (request.args.get('market') or '').strip().upper()
    conn = get_stock_db()
    if not conn:
        return jsonify([]), 200
    try:
        ensure_comments_schema(conn)
        if code:
            rows = conn.execute(
                'SELECT id, nickname, content, created_at, color FROM comments '
                'WHERE code=? AND market=? ORDER BY id DESC LIMIT 100',
                (code, market)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, nickname, content, created_at FROM comments "
                "WHERE code IS NULL OR code='' ORDER BY id DESC LIMIT 100"
            ).fetchall()
        resp = jsonify([dict(r) for r in rows])
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    finally:
        conn.close()


@app.route('/api/comments', methods=['POST'])
def post_comment():
    data     = request.get_json(silent=True) or {}
    content  = (data.get('content') or '').strip()
    nickname = (data.get('nickname') or '匿名').strip()[:20]
    code     = (data.get('code') or '').strip()[:6]
    market   = (data.get('market') or '').strip().upper()
    if not content:
        return jsonify({'error': '留言內容不能為空'}), 400
    if len(content) > 100:
        return jsonify({'error': '留言最多 100 字'}), 400
    if code and not code.isalnum():
        return jsonify({'error': '股票代碼格式錯誤'}), 400
    if market not in ('TSE', 'OTC'):
        market = ''
    color = (data.get('color') or '').strip()
    if color and not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
        color = ''

    conn = get_stock_db()
    if not conn:
        return jsonify({'error': '資料庫尚未初始化'}), 503
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        count_today = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE created_at LIKE ?",
            (today + '%',)
        ).fetchone()[0]
        if count_today >= 500:
            return jsonify({'error': '今日留言已達上限（500則），明天再來吧！'}), 429
        ensure_comments_schema(conn)
        conn.execute(
            'INSERT INTO comments (nickname, content, created_at, code, market, color) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (nickname, content, datetime.utcnow().isoformat() + 'Z',
             code or None, market or None, color or None)
        )
        conn.commit()
        resp = jsonify({'ok': True})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    finally:
        conn.close()


# -- Startup --
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8866))
    print(f'Server started: http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=False)
