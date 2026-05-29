"""
台股注意股・處置股查詢系統 — Flask Web 伺服器
支援上市（TWSE）+ 上櫃（TPEx）股票
本機開發：python app.py
雲端部署：上傳至 Render / Railway，啟動指令 gunicorn app:app
"""

import os
import sqlite3
import requests
from datetime import datetime, timedelta
from flask import Flask, send_from_directory, request, Response, jsonify
from cloud_db import open_db, using_turso

app = Flask(__name__, static_folder='.')

# ── SQLite 快取資料庫 ─────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS yf_price (
            code    TEXT,
            market  TEXT,
            date    TEXT,
            open    REAL,
            high    REAL,
            low     REAL,
            close   REAL,
            volume  INTEGER,
            PRIMARY KEY (code, market, date)
        );
        CREATE TABLE IF NOT EXISTS yf_last_fetch (
            code       TEXT,
            market     TEXT,
            fetched_at TEXT,
            PRIMARY KEY (code, market)
        );
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
    return send_from_directory('.', '注意股處置股查詢系統.html')


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


# ── OTC 股價歷史（Yahoo Finance .TWO，SQLite 快取 4 小時）──

def _safe_float(s):
    try:
        return round(float(str(s).replace(',', '').strip()), 2)
    except Exception:
        return 0.0

def _safe_int(s):
    try:
        return int(str(s).replace(',', '').strip())
    except Exception:
        return 0

YF_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
}

def _fetch_yf_otc(code, days=90):
    """
    從 Yahoo Finance v8 API 抓取上櫃個股歷史日K
    上櫃股票 ticker 格式：{code}.TWO
    回傳 list of {date, open, high, low, close, volume} 或 raise Exception
    """
    ticker = f'{code}.TWO'
    url    = (f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}'
              f'?interval=1d&range={days}d')
    try:
        r = requests.get(url, headers=YF_HEADERS, timeout=20)
        r.raise_for_status()
        payload = r.json()
    except requests.exceptions.RequestException as e:
        raise Exception(f'Yahoo Finance 連線失敗：{e}')

    try:
        result    = payload['chart']['result'][0]
        timestamps = result['timestamp']
        q          = result['indicators']['quote'][0]
        opens      = q.get('open',   [])
        highs      = q.get('high',   [])
        lows       = q.get('low',    [])
        closes     = q.get('close',  [])
        volumes    = q.get('volume', [])
    except (KeyError, IndexError, TypeError):
        # 可能是 chart.error 存在（股票不存在）
        err = payload.get('chart', {}).get('error')
        if err:
            raise Exception(f'Yahoo Finance 錯誤：{err.get("description", err)}')
        raise Exception(f'Yahoo Finance 回傳格式異常')

    rows = []
    for i, ts in enumerate(timestamps):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        from datetime import date as _date, timezone as _tz
        dt_obj  = _date.fromtimestamp(ts)
        iso_dt  = dt_obj.strftime('%Y-%m-%d')
        rows.append({
            'date':   iso_dt,
            'open':   _safe_float(opens[i]   if i < len(opens)   else c),
            'high':   _safe_float(highs[i]   if i < len(highs)   else c),
            'low':    _safe_float(lows[i]    if i < len(lows)    else c),
            'close':  _safe_float(c),
            'volume': _safe_int(volumes[i]   if i < len(volumes) else 0),
        })
    return rows


@app.route('/yf/history')
def yf_history():
    """
    取得上櫃個股近90日日K（供前端使用）
    資料來源：Yahoo Finance（{code}.TWO）
    快取策略：每筆日期資料永久保留；4 小時內重複查詢直接回快取
    """
    code   = request.args.get('code', '').strip()
    market = request.args.get('market', 'TSE').upper()
    if not code:
        return jsonify({'error': 'code required'}), 400
    if market != 'OTC':
        return jsonify({'error': '此端點僅供上櫃（OTC）查詢'}), 400

    now  = datetime.utcnow()
    conn = get_db()

    # 4 小時快取
    cache_row = conn.execute(
        'SELECT fetched_at FROM yf_last_fetch WHERE code=? AND market=?',
        (code, market)
    ).fetchone()

    need_fetch = True
    if cache_row:
        try:
            if (now - datetime.fromisoformat(cache_row['fetched_at'])).total_seconds() < 14400:
                need_fetch = False
        except Exception:
            pass

    if need_fetch:
        try:
            fetched = _fetch_yf_otc(code, days=90)
            if fetched:
                conn.executemany(
                    'INSERT OR REPLACE INTO yf_price '
                    '(code,market,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?,?)',
                    [(code, market, r['date'],
                      r['open'], r['high'], r['low'], r['close'], r['volume'])
                     for r in fetched]
                )
                print(f'[YF] {code}.TWO 取得 {len(fetched)} 個交易日')
            # 僅在成功取到資料時才寫快取時間戳，避免失敗被鎖 4 小時
            if fetched:
                conn.execute(
                    'INSERT OR REPLACE INTO yf_last_fetch (code,market,fetched_at) '
                    'VALUES (?,?,?)',
                    (code, market, now.isoformat())
                )
            conn.commit()
        except Exception as e:
            print(f'[YF] {code}.TWO 抓取失敗: {e}')
            conn.close()
            return jsonify({'error': str(e)}), 502

    rows = conn.execute(
        'SELECT date,open,high,low,close,volume FROM yf_price '
        'WHERE code=? AND market=? ORDER BY date',
        (code, market)
    ).fetchall()
    conn.close()

    if not rows:
        return jsonify({'error': f'查無上櫃 {code} 歷史資料（請確認代碼正確，或此股票不在上櫃一般板）'}), 404

    out  = [{'date': r['date'], 'open': r['open'], 'high': r['high'],
              'low':  r['low'],  'close': r['close'], 'volume': r['volume']}
             for r in rows]
    resp = jsonify(out)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


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

STOCK_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stock_data.db')

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
            '''SELECT date, open, high, low, close, volume, change
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
        return jsonify({'notices': [], 'disposals': []}), 200
    try:
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d')
        notices = conn.execute(
            'SELECT DISTINCT code, name, market FROM notice_stocks '
            'WHERE date >= ? AND (market=? OR ?="") ORDER BY code',
            (cutoff, market, market)
        ).fetchall()
        disposals = conn.execute(
            'SELECT DISTINCT code, name, market FROM disposition_stocks '
            'WHERE (market=? OR ?="") ORDER BY code',
            (market, market)
        ).fetchall()
        resp = jsonify({
            'notices':   [dict(r) for r in notices],
            'disposals': [dict(r) for r in disposals],
        })
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    finally:
        conn.close()


# -- Startup --
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8866))
    print(f'Server started: http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=False)
