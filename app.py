"""
台股注意股・處置股查詢系統 — Flask Web 伺服器
支援上市（TWSE）+ 上櫃（TPEx）股票
本機開發：python app.py
雲端部署：上傳至 Render / Railway，啟動指令 gunicorn app:app
"""

import os
import sqlite3
import requests
from datetime import datetime
from flask import Flask, send_from_directory, request, Response, jsonify

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
YF_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json',
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
        return Response(
            resp.content, status=200,
            content_type='application/json;charset=UTF-8',
            headers={'Access-Control-Allow-Origin': '*'}
        )
    except requests.exceptions.Timeout:
        return jsonify(stat='ERROR', message='TPEx 回應逾時，請稍後再試'), 504
    except requests.exceptions.RequestException as e:
        return jsonify(stat='ERROR', message=str(e)), 502


# ── Yahoo Finance 歷史股價（SQLite 快取 4 小時）─────────
@app.route('/yf/history')
def yf_history():
    code   = request.args.get('code', '').strip()
    market = request.args.get('market', 'TSE').upper()
    if not code:
        return jsonify({'error': 'code required'}), 400

    suffix = '.TW' if market == 'TSE' else '.TWO'
    symbol = code + suffix
    now    = datetime.utcnow()
    conn   = get_db()

    # 確認是否需要重新抓取（快取 4 小時）
    row = conn.execute(
        'SELECT fetched_at FROM yf_last_fetch WHERE code=? AND market=?',
        (code, market)
    ).fetchone()

    need_fetch = True
    if row:
        try:
            last_dt = datetime.fromisoformat(row['fetched_at'])
            if (now - last_dt).total_seconds() < 14400:
                need_fetch = False
        except Exception:
            pass

    if need_fetch:
        try:
            url  = (
                f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
                f'?range=3mo&interval=1d'
            )
            resp = requests.get(url, headers=YF_HEADERS, timeout=15)
            resp.raise_for_status()
            data   = resp.json()
            result = (data.get('chart') or {}).get('result') or []

            if result:
                r          = result[0]
                timestamps = r.get('timestamp') or []
                q          = ((r.get('indicators') or {}).get('quote') or [{}])[0]
                opens  = q.get('open')   or []
                highs  = q.get('high')   or []
                lows   = q.get('low')    or []
                closes = q.get('close')  or []
                vols   = q.get('volume') or []

                to_ins = []
                for i, ts in enumerate(timestamps):
                    dt  = datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d')
                    o   = opens[i]  if i < len(opens)  else None
                    h   = highs[i]  if i < len(highs)  else None
                    l   = lows[i]   if i < len(lows)   else None
                    cl  = closes[i] if i < len(closes) else None
                    vol = vols[i]   if i < len(vols)   else None
                    if cl is not None:
                        to_ins.append((
                            code, market, dt,
                            round(float(o),  2) if o  else None,
                            round(float(h),  2) if h  else None,
                            round(float(l),  2) if l  else None,
                            round(float(cl), 2),
                            int(vol) if vol else 0
                        ))
                if to_ins:
                    conn.executemany(
                        'INSERT OR REPLACE INTO yf_price '
                        '(code,market,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?,?)',
                        to_ins
                    )

            conn.execute(
                'INSERT OR REPLACE INTO yf_last_fetch (code,market,fetched_at) VALUES (?,?,?)',
                (code, market, now.isoformat())
            )
            conn.commit()
        except Exception as e:
            print(f'[YF] fetch error for {symbol}: {e}')

    # 回傳快取資料
    rows = conn.execute(
        'SELECT date,open,high,low,close,volume FROM yf_price '
        'WHERE code=? AND market=? ORDER BY date',
        (code, market)
    ).fetchall()
    conn.close()

    if not rows:
        return jsonify({'error': f'查無 {code}（{market}）歷史資料'}), 404

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


# ── 啟動 ──────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8866))
    print(f'Server started: http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=False)
