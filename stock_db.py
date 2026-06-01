"""
台股注意股/處置股 資料庫更新腳本
支援上市（TSE）+ 上櫃（OTC）

功能：
  1. 每日自動抓取 TWSE / TPEx 的注意股、處置股名單
  2. 抓取名單內股票的近 20 個營業日歷史股價
  3. 刪除超過 30 天的舊資料
  4. 記錄每次更新狀態

用法：
  python stock_db.py            # 執行一次完整更新
  python stock_db.py --init     # 僅初始化資料庫（不抓資料）
  python stock_db.py --status   # 顯示最後更新狀態

雲端資料庫（Turso）：
  設定環境變數後自動連 Turso，否則使用本機 SQLite：
  TURSO_DATABASE_URL = libsql://your-db.turso.io
  TURSO_AUTH_TOKEN   = your-token
"""

import os
import sys
import time
import warnings
import requests
import urllib3
from datetime import datetime, timedelta
from cloud_db import open_db, using_turso

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── 設定 ─────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stock_data.db')

# 保留天數（90 日曆天 ≈ 60 個交易日，供 60日均量計算使用）
KEEP_DAYS = 90

TWSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer':    'https://www.twse.com.tw/',
    'Accept':     'application/json, text/plain, */*',
}
TPEX_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer':    'https://www.tpex.org.tw/',
    'Accept':     'application/json, text/plain, */*',
}

TPEX_BASE = 'https://www.tpex.org.tw/openapi/v1'


# ═══════════════════════════════════════════════
# 資料庫管理
# ═══════════════════════════════════════════════

def get_db():
    return open_db(DB_PATH, pragma_wal=True)


def init_db():
    """建立所有資料表（若不存在）"""
    conn = get_db()
    conn.executescript('''
        -- 股票基本資料（上市+上櫃）
        CREATE TABLE IF NOT EXISTS stocks (
            code    TEXT,
            name    TEXT,
            market  TEXT CHECK(market IN ('TSE','OTC')),
            updated TEXT,
            PRIMARY KEY (code, market)
        );

        -- 每日股價（只保留近90日）
        CREATE TABLE IF NOT EXISTS daily_price (
            code    TEXT,
            market  TEXT,
            date    TEXT,   -- YYYY-MM-DD
            open    REAL,
            high    REAL,
            low     REAL,
            close   REAL,
            volume  INTEGER,
            change  REAL,   -- 漲跌額
            PRIMARY KEY (code, market, date)
        );
        CREATE INDEX IF NOT EXISTS idx_price_date   ON daily_price(date);
        CREATE INDEX IF NOT EXISTS idx_price_code   ON daily_price(code, market);

        -- 注意股歷史記錄（近90日）
        CREATE TABLE IF NOT EXISTS notice_stocks (
            code    TEXT,
            name    TEXT,
            market  TEXT,
            date    TEXT,   -- YYYY-MM-DD
            reason  TEXT,
            close   REAL,
            PRIMARY KEY (code, market, date)
        );
        CREATE INDEX IF NOT EXISTS idx_notice_code ON notice_stocks(code, market);

        -- 處置股現況（隨每次更新替換）
        CREATE TABLE IF NOT EXISTS disposition_stocks (
            code          TEXT,
            name          TEXT,
            market        TEXT,
            announce_date TEXT,
            start_date    TEXT,
            end_date      TEXT,
            reason        TEXT,
            measure       TEXT,
            content       TEXT,
            PRIMARY KEY (code, market, announce_date)
        );

        -- 每日更新日誌
        CREATE TABLE IF NOT EXISTS update_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            updated_at TEXT,
            status     TEXT,
            message    TEXT
        );
    ''')
    conn.commit()
    conn.close()
    if using_turso():
        print('[DB] 資料庫初始化完成：Turso cloud')
    else:
        print('[DB] 資料庫初始化完成:', DB_PATH)


# ═══════════════════════════════════════════════
# 日期工具
# ═══════════════════════════════════════════════

def roc_slash_to_iso(s):
    """'115/05/28' 或 '115.05.28' → '2026-05-28'"""
    try:
        s = str(s).strip().replace('.', '/')
        parts = s.split('/')
        if len(parts) == 3:
            return f'{int(parts[0])+1911}-{parts[1].zfill(2)}-{parts[2].zfill(2)}'
    except Exception:
        pass
    return ''


def roc8_to_iso(s):
    """'1150528' → '2026-05-28'"""
    try:
        s = str(s).strip().zfill(7)
        return f'{int(s[:3])+1911}-{s[3:5]}-{s[5:7]}'
    except Exception:
        return ''


def get_past_months(n=3):
    """回傳過去 n 個月的 (year, month) 清單，由舊到新"""
    today = datetime.now()
    result, seen = [], set()
    for i in range(n - 1, -1, -1):
        # 往前推 i 個月
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        key = (y, m)
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def safe_float(s, default=0.0):
    try:
        return float(str(s).replace(',', '').replace('--', '').strip() or default)
    except Exception:
        return default


def safe_int(s, default=0):
    try:
        return int(str(s).replace(',', '').strip() or default)
    except Exception:
        return default


# ═══════════════════════════════════════════════
# TSE（上市）資料抓取
# ═══════════════════════════════════════════════

def fetch_tse_notice(days=30):
    """抓取 TWSE 全市場注意股記錄（近 days 天，單次 API）
    回傳格式：[編號, 代號, 名稱, 累計次數, 注意資訊, 日期"115/05/28", 收盤, 本益比]
    """
    today = datetime.now()
    end   = today - timedelta(days=1)
    start = today - timedelta(days=days)
    try:
        r = requests.get(
            'https://www.twse.com.tw/announcement/notice',
            params={
                'response':  'json',
                'querytype': '1',
                'startDate': start.strftime('%Y%m%d'),
                'endDate':   end.strftime('%Y%m%d'),
            },
            headers=TWSE_HEADERS, timeout=20
        )
        r.raise_for_status()
        d = r.json()
        if d.get('stat') == 'OK' and d.get('data'):
            return d['data']
    except Exception as e:
        print(f'  [TSE notice all] 失敗: {e}')
    return []


def fetch_tse_notice_for_codes(codes, days=65):
    """
    針對指定代碼逐一查詢 TWSE 注意股記錄（querytype=2 + stockNo）
    回傳格式：[序號, 代碼, 名稱, 累計, 原因, 日期, 收盤, PE]
    """
    today = datetime.now()
    end   = today - timedelta(days=1)
    start = today - timedelta(days=days)
    all_rows = []
    for code in sorted(codes):
        try:
            r = requests.get(
                'https://www.twse.com.tw/announcement/notice',
                params={
                    'response':  'json',
                    'querytype': '2',
                    'startDate': start.strftime('%Y%m%d'),
                    'endDate':   end.strftime('%Y%m%d'),
                    'stockNo':   code,
                },
                headers=TWSE_HEADERS, timeout=20
            )
            r.raise_for_status()
            d = r.json()
            if d.get('stat') == 'OK' and d.get('data'):
                all_rows.extend(d['data'])
            time.sleep(0.3)
        except Exception as e:
            print(f'  [TSE notice] {code} 失敗: {e}')
    return all_rows


def fetch_tse_disposal():
    """抓取 TWSE 現行處置股清單"""
    try:
        r = requests.get(
            'https://www.twse.com.tw/announcement/punish',
            params={'response': 'json'},
            headers=TWSE_HEADERS, timeout=20
        )
        r.raise_for_status()
        d = r.json()
        if d.get('stat') == 'OK' and d.get('data'):
            return d['data']
    except Exception as e:
        print(f'  [TSE disposal] 失敗: {e}')
    return []


def fetch_tse_month_prices(code, year, month):
    """
    抓取 TWSE 個股單月日K
    回傳 list of dict: {date, open, high, low, close, volume, change}
    """
    params = {
        'response': 'json',
        'date':     f'{year}{str(month).zfill(2)}01',
        'stockNo':  code,
    }
    try:
        r = requests.get(
            'https://www.twse.com.tw/exchangeReport/STOCK_DAY',
            params=params, headers=TWSE_HEADERS, timeout=20
        )
        r.raise_for_status()
        d = r.json()
        if d.get('stat') != 'OK' or not d.get('data'):
            return []
        rows = []
        for row in d['data']:
            # [日期, 成交股數, 成交金額, 開盤, 最高, 最低, 收盤, 漲跌, 筆數]
            date = roc_slash_to_iso(row[0])
            if not date:
                continue
            rows.append({
                'date':   date,
                'open':   safe_float(row[3]),
                'high':   safe_float(row[4]),
                'low':    safe_float(row[5]),
                'close':  safe_float(row[6]),
                'volume': safe_int(row[1]),
                'change': safe_float(row[7]),
            })
        return rows
    except Exception as e:
        print(f'  [TSE price] {code} {year}/{month:02d} 失敗: {e}')
        return []


# ═══════════════════════════════════════════════
# OTC（上櫃）資料抓取
# ═══════════════════════════════════════════════

def fetch_otc_notice(days=30):
    """
    抓取 TPEx 注意股記錄（支援日期範圍，近 days 天）
    URL: /www/zh-tw/bulletin/attention?startDate=YYYY/MM/DD&endDate=YYYY/MM/DD&response=json
    欄位: [序號, 代號, 名稱, 累計, 原因, 發生日"115/05/29", 收盤, 本益比, link]
    回傳 list of rows
    """
    today = datetime.now()
    start = today - timedelta(days=days)
    try:
        r = requests.get(
            'https://www.tpex.org.tw/www/zh-tw/bulletin/attention',
            params={
                'startDate': start.strftime('%Y/%m/%d'),
                'endDate':   today.strftime('%Y/%m/%d'),
                'code': '', 'cate': '', 'type': 'all',
                'order': 'date', 'id': '', 'response': 'json',
            },
            headers=TPEX_HEADERS, timeout=20, verify=False
        )
        r.raise_for_status()
        d = r.json()
        tables = d.get('tables', [])
        if tables and tables[0].get('data'):
            return tables[0]['data']
    except Exception as e:
        print(f'  [OTC notice] 失敗: {e}')
    return []


def fetch_otc_disposal():
    """抓取 TPEx 現行處置股清單"""
    try:
        r = requests.get(
            f'{TPEX_BASE}/tpex_disposal_information',
            headers=TPEX_HEADERS, timeout=20, verify=False
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f'  [OTC disposal] 失敗: {e}')
    return []


def iso_to_roc7(iso_date):
    """'2026-05-28' → '1150528'（TPEx 日期格式）"""
    y, m, d = iso_date.split('-')
    return f'{int(y)-1911}{m}{d}'


def fetch_otc_daily_all(iso_date):
    """
    抓取 TPEx 單日所有上櫃股票行情（支援歷史日期）
    URL: /www/zh-tw/afterTrading/dailyQuotes?date=YYYY/MM/DD&response=json
    欄位: [0]代號 [1]名稱 [2]收盤 [3]漲跌 [4]開盤 [5]最高 [6]最低 [8]成交量(股)
    回傳 dict: {code → {date, open, high, low, close, volume, change}}
    若非交易日（無資料）則回傳空 dict
    """
    # 轉成 YYYY/MM/DD 格式（西元年，此 API 用西元年不用民國年）
    greg_date = iso_date.replace('-', '/')
    try:
        r = requests.get(
            'https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes',
            params={'date': greg_date, 'id': '', 'response': 'json'},
            headers=TPEX_HEADERS, timeout=20, verify=False
        )
        r.raise_for_status()
        d = r.json()
        tables = d.get('tables', [])
        if not tables or not tables[0].get('data'):
            return {}
        rows = tables[0]['data']
        result = {}
        for row in rows:
            if len(row) < 9:
                continue
            code = str(row[0]).strip()
            if not (len(code) == 4 and code.isdigit()):
                continue
            result[code] = {
                'name':   str(row[1]).strip(),
                'date':   iso_date,
                'close':  safe_float(row[2]),
                'change': safe_float(row[3]),
                'open':   safe_float(row[4]),
                'high':   safe_float(row[5]),
                'low':    safe_float(row[6]),
                'volume': safe_int(str(row[8]).replace(',', '')) // 1000,  # 股→張
            }
        return result
    except Exception as e:
        msg = str(e)
        if 'prematurely' not in msg and 'RemoteDisconnected' not in msg:
            print(f'  [OTC daily] {iso_date} 失敗: {e}')
        return {}


def fetch_tse_daily_all(iso_date):
    """
    使用 TWSE MI_INDEX 批次抓取指定日期所有上市股票收盤行情
    一次 API 呼叫取得全部上市股票（約 900+ 支）
    回傳 dict: {code → {date, open, high, low, close, volume, change}}
    """
    date_str = iso_date.replace('-', '')
    try:
        r = requests.get(
            'https://www.twse.com.tw/exchangeReport/MI_INDEX',
            params={'response': 'json', 'date': date_str, 'type': 'ALLBUT0999'},
            headers=TWSE_HEADERS, timeout=20
        )
        r.raise_for_status()
        d = r.json()
        if d.get('stat') != 'OK':
            return {}

        # 個股資料在 tables 裡欄位最多（≥10）且筆數最多的那張子表
        stock_table = None
        for t in d.get('tables', []):
            if len(t.get('fields', [])) >= 10 and len(t.get('data', [])) > 100:
                stock_table = t
                break
        if not stock_table:
            return {}

        result = {}
        for row in stock_table['data']:
            if len(row) < 11:
                continue
            code = str(row[0]).strip()
            if not (len(code) == 4 and code.isdigit()):
                continue
            # row[9] 是 HTML：<p style= color:red>+</p> 或 color:green>-</p>
            direction_html = str(row[9])
            if 'red' in direction_html:
                sign = 1
            elif 'green' in direction_html:
                sign = -1
            else:
                sign = 0
            chg = safe_float(str(row[10]).replace(',', '')) * sign
            result[code] = {
                'name':   str(row[1]).strip(),
                'date':   iso_date,
                'open':   safe_float(str(row[5]).replace(',', '')),
                'high':   safe_float(str(row[6]).replace(',', '')),
                'low':    safe_float(str(row[7]).replace(',', '')),
                'close':  safe_float(str(row[8]).replace(',', '')),
                'volume': safe_int(str(row[2]).replace(',', '')) // 1000,  # 股→張
                'change': chg,
            }
        return result
    except Exception as e:
        msg = str(e)
        if 'prematurely' not in msg and 'RemoteDisconnected' not in msg:
            print(f'  [TSE bulk daily] {iso_date} 失敗: {e}')
        return {}


def fetch_all_prices_bulk(days=90):
    """
    批次抓取全市場（TSE + OTC）近 days 個日曆天的股價
    每個交易日只需 2 次 API（TSE MI_INDEX + OTC mainboard）
    回傳 (tse_data, otc_data)
      tse_data / otc_data: {code → [records sorted oldest→newest]}
    """
    today    = datetime.now().date()
    tse_data  = {}
    otc_data  = {}
    tse_names = {}   # {code: name}
    otc_names = {}
    t_days    = 0

    for offset in range(days):
        target = today - timedelta(days=offset)
        if target.weekday() >= 5:
            continue
        iso = target.strftime('%Y-%m-%d')

        tse_day = fetch_tse_daily_all(iso)
        otc_day = fetch_otc_daily_all(iso)

        if tse_day or otc_day:
            t_days += 1

        for code, data in tse_day.items():
            tse_data.setdefault(code, []).append(data)
            if data.get('name') and code not in tse_names:
                tse_names[code] = data['name']
        for code, data in otc_day.items():
            otc_data.setdefault(code, []).append(data)
            if data.get('name') and code not in otc_names:
                otc_names[code] = data['name']

        time.sleep(0.3)

    for d in (tse_data, otc_data):
        for code in d:
            d[code].sort(key=lambda r: r['date'])

    print(f'  [全市場] {t_days} 個交易日｜TSE {len(tse_data)} 支｜OTC {len(otc_data)} 支')
    return tse_data, otc_data, tse_names, otc_names


# ═══════════════════════════════════════════════
# 資料庫寫入工具
# ═══════════════════════════════════════════════

def upsert_stock(conn, code, name, market):
    conn.execute(
        'INSERT OR REPLACE INTO stocks (code, name, market, updated) VALUES (?,?,?,?)',
        (code, name, market, datetime.now().isoformat())
    )


def bulk_insert_prices(conn, market_data, market):
    """
    一次性把全市場股價寫入 DB（減少 HTTP 請求次數）
    market_data: {code → [row_dicts]}
    """
    all_rows = [
        (code, market,
         r['date'], r['open'], r['high'], r['low'],
         r['close'], r['volume'], r.get('change', 0.0))
        for code, rows in market_data.items()
        for r in rows
        if r.get('close', 0) > 0  # 過濾無效資料
    ]
    if not all_rows:
        return 0
    conn.executemany(
        'INSERT OR REPLACE INTO daily_price '
        '(code,market,date,open,high,low,close,volume,change) VALUES (?,?,?,?,?,?,?,?,?)',
        all_rows
    )
    return len(all_rows)


def insert_prices(conn, code, market, rows):
    if not rows:
        return 0
    data = [
        (code, market,
         r['date'], r['open'], r['high'], r['low'],
         r['close'], r['volume'], r.get('change', 0.0))
        for r in rows
    ]
    conn.executemany(
        'INSERT OR REPLACE INTO daily_price '
        '(code,market,date,open,high,low,close,volume,change) VALUES (?,?,?,?,?,?,?,?,?)',
        data
    )
    return len(data)


def cleanup(conn, keep_days=None):
    """清除超過 keep_days 天的股價與注意股記錄"""
    days = keep_days if keep_days is not None else KEEP_DAYS
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    c1 = conn.execute('DELETE FROM daily_price   WHERE date < ?', (cutoff,)).rowcount
    c2 = conn.execute('DELETE FROM notice_stocks WHERE date < ?', (cutoff,)).rowcount
    return c1, c2


# ═══════════════════════════════════════════════
# 主更新流程
# ═══════════════════════════════════════════════

def update_all(verbose=True):
    def log(msg):
        if verbose:
            print(msg)

    start_time = datetime.now()
    log(f'\n{"="*55}')
    log(f' 台股資料庫更新  {start_time.strftime("%Y-%m-%d %H:%M:%S")}')
    log(f'{"="*55}')

    conn   = get_db()
    stats  = dict(tse_notice=0, otc_notice=0,
                  tse_disposal=0, otc_disposal=0,
                  tse_price=0, otc_price=0)
    tse_codes = set()
    otc_codes = set()
    now_str   = datetime.now().isoformat()

    # ── Step 1：TSE 注意股（全市場，近30日，單次 API）─────
    log('\n[1/6] TSE 注意股（TWSE，全市場近30日）...')
    tse_n_stocks, tse_n_rows = [], []
    for row in fetch_tse_notice(days=30):
        # row: [編號, 代號, 名稱, 累計次數, 注意資訊, 日期"115/05/28", 收盤, 本益比]
        if len(row) < 6:
            continue
        code = str(row[1]).strip()
        if len(code) != 4 or not code.isdigit():
            continue
        name      = str(row[2]).strip()
        date      = roc_slash_to_iso(row[5])
        if not date:
            continue
        reason    = str(row[4]).strip() if len(row) > 4 else ''
        close_val = safe_float(row[6]) if len(row) > 6 else 0.0
        tse_codes.add(code)
        tse_n_stocks.append((code, name, 'TSE', now_str))
        tse_n_rows.append((code, name, 'TSE', date, reason, close_val))
    if tse_n_stocks:
        conn.executemany(
            'INSERT OR REPLACE INTO stocks (code,name,market,updated) VALUES (?,?,?,?)',
            tse_n_stocks
        )
    if tse_n_rows:
        conn.executemany(
            'INSERT OR REPLACE INTO notice_stocks '
            '(code,name,market,date,reason,close) VALUES (?,?,?,?,?,?)',
            tse_n_rows
        )
    conn.commit()
    stats['tse_notice'] = len(tse_n_rows)
    log(f'  → {stats["tse_notice"]} 筆，{len(tse_codes)} 支股票')

    # ── Step 2：TSE 處置股 ─────────────────────
    time.sleep(2)   # 避免連續打 TWSE 被 rate limit
    log('\n[2/6] TSE 處置股（TWSE）...')
    conn.execute("DELETE FROM disposition_stocks WHERE market='TSE'")
    conn.commit()
    tse_disp_codes = set()
    tse_d_stocks, tse_d_rows = [], []
    for row in fetch_tse_disposal():
        # row: [序號, 公告日, 代碼, 名稱, 第N次, 原因, 期間, 措施, 內容]
        if len(row) < 4:
            continue
        code = str(row[2]).strip()
        name = str(row[3]).strip()
        if not code:
            continue
        announce = roc_slash_to_iso(row[1]) if len(row) > 1 else ''
        period   = str(row[6]) if len(row) > 6 else ''
        sd = ed = ''
        if '～' in period or '~' in period:
            parts = period.replace('~', '～').split('～')
            sd = roc_slash_to_iso(parts[0])
            ed = roc_slash_to_iso(parts[1]) if len(parts) > 1 else ''
        tse_disp_codes.add(code)
        tse_codes.add(code)
        tse_d_stocks.append((code, name, 'TSE', now_str))
        tse_d_rows.append((code, name, 'TSE', announce, sd, ed,
                           str(row[5]) if len(row) > 5 else '',
                           str(row[7]) if len(row) > 7 else '',
                           str(row[8]) if len(row) > 8 else ''))
    if tse_d_stocks:
        conn.executemany(
            'INSERT OR REPLACE INTO stocks (code,name,market,updated) VALUES (?,?,?,?)',
            tse_d_stocks
        )
    if tse_d_rows:
        conn.executemany(
            'INSERT OR REPLACE INTO disposition_stocks '
            '(code,name,market,announce_date,start_date,end_date,reason,measure,content) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            tse_d_rows
        )
    conn.commit()
    stats['tse_disposal'] = len(tse_d_rows)
    log(f'  → {stats["tse_disposal"]} 筆，{len(tse_disp_codes)} 支股票')

    # ── Step 3：OTC 注意股 ─────────────────────
    log('\n[3/6] OTC 注意股（TPEx，近30日）...')
    otc_n_stocks, otc_n_rows = [], []
    for row in fetch_otc_notice(days=30):
        # row: [序號, 代號, 名稱, 累計, 原因, 發生日"115/05/29", 收盤, 本益比, link]
        if len(row) < 7:
            continue
        code = str(row[1]).strip()
        if len(code) != 4 or not code.isdigit():
            continue
        name      = str(row[2]).strip()
        date      = roc_slash_to_iso(row[5])
        if not date:
            continue
        reason    = str(row[4]).strip()
        close_val = safe_float(row[6])
        otc_codes.add(code)
        otc_n_stocks.append((code, name, 'OTC', now_str))
        otc_n_rows.append((code, name, 'OTC', date, reason, close_val))
    if otc_n_stocks:
        conn.executemany(
            'INSERT OR REPLACE INTO stocks (code,name,market,updated) VALUES (?,?,?,?)',
            otc_n_stocks
        )
    if otc_n_rows:
        conn.executemany(
            'INSERT OR REPLACE INTO notice_stocks '
            '(code,name,market,date,reason,close) VALUES (?,?,?,?,?,?)',
            otc_n_rows
        )
    conn.commit()
    stats['otc_notice'] = len(otc_n_rows)
    log(f'  → {stats["otc_notice"]} 筆，{len(otc_codes)} 支股票')

    # ── Step 4：OTC 處置股 ─────────────────────
    log('\n[4/6] OTC 處置股（TPEx）...')
    conn.execute("DELETE FROM disposition_stocks WHERE market='OTC'")
    conn.commit()
    otc_disp_codes = set()
    otc_d_stocks, otc_d_rows = [], []
    for item in fetch_otc_disposal():
        code = str(item.get('SecuritiesCompanyCode', '')).strip()
        name = str(item.get('CompanyName', '')).strip()
        if not code:
            continue
        announce = roc8_to_iso(item.get('Date', ''))
        period   = str(item.get('DispositionPeriod', ''))
        sd = ed = ''
        if '~' in period:
            parts = period.split('~')
            sd = roc8_to_iso(parts[0])
            ed = roc8_to_iso(parts[1]) if len(parts) > 1 else ''
        otc_disp_codes.add(code)
        otc_codes.add(code)
        otc_d_stocks.append((code, name, 'OTC', now_str))
        otc_d_rows.append((code, name, 'OTC', announce, sd, ed,
                           str(item.get('DispositionReasons', '')),
                           str(item.get('DispositionMeasure', '')),
                           str(item.get('DisposalCondition', ''))))
    if otc_d_stocks:
        conn.executemany(
            'INSERT OR REPLACE INTO stocks (code,name,market,updated) VALUES (?,?,?,?)',
            otc_d_stocks
        )
    if otc_d_rows:
        conn.executemany(
            'INSERT OR REPLACE INTO disposition_stocks '
            '(code,name,market,announce_date,start_date,end_date,reason,measure,content) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            otc_d_rows
        )
    conn.commit()
    stats['otc_disposal'] = len(otc_d_rows)
    log(f'  → {stats["otc_disposal"]} 筆，{len(otc_disp_codes)} 支股票')

    # ── Step 5+6：全市場股價（TSE MI_INDEX + OTC bulk，每日僅 2 次 API）──
    log('\n[5/6] 全市場股價歷史（TSE + OTC 批次抓取）...')
    # 若 DB 已有 60 個交易日資料，只補最近 7 個日曆天（涵蓋週末 buffer）
    # 若不足則做一次完整 90 天補齊
    existing_trade_days = conn.execute(
        "SELECT COUNT(DISTINCT date) FROM daily_price WHERE market='TSE'"
    ).fetchone()[0]
    fetch_days = 7 if existing_trade_days >= 60 else 90
    log(f'  DB 現有 {existing_trade_days} 個交易日 → 抓取最近 {fetch_days} 個日曆天')
    tse_all, otc_all, tse_names, otc_names = fetch_all_prices_bulk(days=fetch_days)

    # 批次寫入股票名稱（供自動完成使用）
    now_str = datetime.now().isoformat()
    name_rows = (
        [(code, name, 'TSE', now_str) for code, name in tse_names.items() if name] +
        [(code, name, 'OTC', now_str) for code, name in otc_names.items() if name]
    )
    if name_rows:
        conn.executemany(
            'INSERT OR REPLACE INTO stocks (code,name,market,updated) VALUES (?,?,?,?)',
            name_rows
        )
        conn.commit()
        log(f'  寫入股票名稱：TSE {len(tse_names)} + OTC {len(otc_names)} 支')

    log(f'  寫入 TSE {len(tse_all)} 支股價...')
    stats['tse_price'] = bulk_insert_prices(conn, tse_all, 'TSE')
    conn.commit()
    log(f'  → TSE 共 {stats["tse_price"]} 筆')

    log(f'  寫入 OTC {len(otc_all)} 支股價...')
    stats['otc_price'] = bulk_insert_prices(conn, otc_all, 'OTC')
    conn.commit()
    log(f'\n[6/6] → OTC 共 {stats["otc_price"]} 筆')

    # ── 清理舊資料 ────────────────────────────
    dp, dn = cleanup(conn)
    conn.commit()
    log(f'\n[CLEANUP] 清除 {dp} 筆舊股價、{dn} 筆舊注意記錄')

    # ── 寫入日誌 ──────────────────────────────
    elapsed = (datetime.now() - start_time).total_seconds()
    msg = (f"TSE注意:{stats['tse_notice']} OTC注意:{stats['otc_notice']} "
           f"TSE處置:{stats['tse_disposal']} OTC處置:{stats['otc_disposal']} "
           f"TSE股價:{stats['tse_price']} OTC股價:{stats['otc_price']} "
           f"耗時:{elapsed:.0f}s")
    conn.execute(
        'INSERT INTO update_log (updated_at, status, message) VALUES (?,?,?)',
        (datetime.now().isoformat(), 'OK', msg)
    )
    conn.commit()
    conn.close()

    log(f'\n{"="*55}')
    log(f' 更新完成！耗時 {elapsed:.1f} 秒')
    log(f' {msg}')
    log(f'{"="*55}\n')
    return stats


def update_prices_only(verbose=True):
    """只更新股價，不動注意股/處置股（供收盤後 14:30 使用）"""
    def log(msg):
        if verbose:
            print(msg)

    start_time = datetime.now()
    log(f'\n{"="*55}')
    log(f' 股價更新（僅股價）  {start_time.strftime("%Y-%m-%d %H:%M:%S")}')
    log(f'{"="*55}')

    conn = get_db()

    existing_trade_days = conn.execute(
        "SELECT COUNT(DISTINCT date) FROM daily_price WHERE market='TSE'"
    ).fetchone()[0]
    fetch_days = 7 if existing_trade_days >= 60 else 90
    log(f'  DB 現有 {existing_trade_days} 個交易日 → 抓取最近 {fetch_days} 個日曆天')

    tse_all, otc_all, tse_names, otc_names = fetch_all_prices_bulk(days=fetch_days)

    now_str = datetime.now().isoformat()
    name_rows = (
        [(code, name, 'TSE', now_str) for code, name in tse_names.items() if name] +
        [(code, name, 'OTC', now_str) for code, name in otc_names.items() if name]
    )
    if name_rows:
        conn.executemany(
            'INSERT OR REPLACE INTO stocks (code,name,market,updated) VALUES (?,?,?,?)',
            name_rows
        )
        conn.commit()
        log(f'  寫入股票名稱：TSE {len(tse_names)} + OTC {len(otc_names)} 支')

    log(f'  寫入 TSE {len(tse_all)} 支股價...')
    tse_price = bulk_insert_prices(conn, tse_all, 'TSE')
    conn.commit()
    log(f'  → TSE 共 {tse_price} 筆')

    log(f'  寫入 OTC {len(otc_all)} 支股價...')
    otc_price = bulk_insert_prices(conn, otc_all, 'OTC')
    conn.commit()
    log(f'  → OTC 共 {otc_price} 筆')

    dp, dn = cleanup(conn)
    conn.commit()
    log(f'\n[CLEANUP] 清除 {dp} 筆舊股價、{dn} 筆舊注意記錄')

    elapsed = (datetime.now() - start_time).total_seconds()
    msg = f'TSE股價:{tse_price} OTC股價:{otc_price} 耗時:{elapsed:.0f}s'
    conn.execute(
        'INSERT INTO update_log (updated_at, status, message) VALUES (?,?,?)',
        (datetime.now().isoformat(), 'OK(prices-only)', msg)
    )
    conn.commit()
    conn.close()

    log(f'\n{"="*55}')
    log(f' 股價更新完成！耗時 {elapsed:.1f} 秒')
    log(f' {msg}')
    log(f'{"="*55}\n')


def show_status():
    """顯示資料庫現況"""
    if not os.path.exists(DB_PATH):
        print('資料庫尚未建立，請先執行: python stock_db.py --init')
        return
    conn = get_db()
    print('\n── 資料庫狀態 ──────────────────────────')
    print(f'  路徑: {DB_PATH}')

    # 最後更新
    row = conn.execute(
        'SELECT updated_at, status, message FROM update_log ORDER BY id DESC LIMIT 1'
    ).fetchone()
    if row:
        print(f'  最後更新: {row["updated_at"][:19]}  [{row["status"]}]')
        print(f'  摘要: {row["message"]}')
    else:
        print('  尚無更新記錄')

    # 資料筆數
    for table in ('daily_price', 'notice_stocks', 'disposition_stocks', 'stocks'):
        cnt = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        print(f'  {table}: {cnt} 筆')

    # 最新/最舊日期
    row = conn.execute(
        'SELECT MIN(date) AS oldest, MAX(date) AS newest FROM daily_price'
    ).fetchone()
    if row and row['oldest']:
        print(f'  股價日期範圍: {row["oldest"]} ~ {row["newest"]}')

    conn.close()
    print()


# ═══════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════

if __name__ == '__main__':
    args = sys.argv[1:]

    if '--reset' in args:
        print('清空所有資料表...')
        conn = get_db()
        for table in ('daily_price', 'notice_stocks', 'disposition_stocks',
                      'stocks', 'update_log'):
            conn.execute(f'DELETE FROM {table}')
        conn.commit()
        conn.close()
        print('清空完成，重新初始化...')
        init_db()
    elif '--init' in args:
        init_db()
    elif '--status' in args:
        init_db()
        show_status()
    elif '--test-bulk' in args:
        # 小量測試：只抓 3 個交易日，估算完整執行時間
        init_db()
        import time as _time
        print('測試抓取 3 天全市場股價...')
        t0 = _time.time()
        tse_all, otc_all = fetch_all_prices_bulk(days=5)  # 5 日曆天 ≈ 3 個交易日
        fetch_sec = _time.time() - t0
        print(f'  抓取耗時：{fetch_sec:.1f} 秒')

        conn = get_db()
        t1 = _time.time()
        sample_tse = dict(list(tse_all.items())[:200])
        sample_otc = dict(list(otc_all.items())[:200])
        tse_n = bulk_insert_prices(conn, sample_tse, 'TSE')
        otc_n = bulk_insert_prices(conn, sample_otc, 'OTC')
        conn.commit()
        write_sec = _time.time() - t1
        conn.close()

        tse_total = len(tse_all)
        otc_total = len(otc_all)
        total_rows = sum(len(v) for v in tse_all.values()) + sum(len(v) for v in otc_all.values())
        sample_rows = tse_n + otc_n
        est_write = write_sec / sample_rows * total_rows if sample_rows else 0
        print(f'  寫入 {sample_rows} 筆耗時：{write_sec:.1f} 秒')
        print(f'  全市場 TSE {tse_total} + OTC {otc_total} = {tse_total+otc_total} 支')
        print(f'  全市場總筆數：{total_rows:,}')
        print(f'  預估完整寫入時間：{est_write:.0f} 秒（{est_write/60:.1f} 分鐘）')
    elif '--step2' in args:
        init_db()
        conn = get_db()
        now_str = datetime.now().isoformat()
        print('[Step 2] TSE 處置股...')
        conn.execute("DELETE FROM disposition_stocks WHERE market='TSE'")
        conn.commit()
        tse_d_stocks, tse_d_rows = [], []
        for row in fetch_tse_disposal():
            if len(row) < 4:
                continue
            code = str(row[2]).strip()
            name = str(row[3]).strip()
            if not code:
                continue
            announce = roc_slash_to_iso(row[1]) if len(row) > 1 else ''
            period   = str(row[6]) if len(row) > 6 else ''
            sd = ed = ''
            if '～' in period or '~' in period:
                parts = period.replace('~', '～').split('～')
                sd = roc_slash_to_iso(parts[0])
                ed = roc_slash_to_iso(parts[1]) if len(parts) > 1 else ''
            tse_d_stocks.append((code, name, 'TSE', now_str))
            tse_d_rows.append((code, name, 'TSE', announce, sd, ed,
                               str(row[5]) if len(row) > 5 else '',
                               str(row[7]) if len(row) > 7 else '',
                               str(row[8]) if len(row) > 8 else ''))
        if tse_d_stocks:
            conn.executemany(
                'INSERT OR REPLACE INTO stocks (code,name,market,updated) VALUES (?,?,?,?)',
                tse_d_stocks
            )
        if tse_d_rows:
            conn.executemany(
                'INSERT OR REPLACE INTO disposition_stocks '
                '(code,name,market,announce_date,start_date,end_date,reason,measure,content) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                tse_d_rows
            )
        conn.commit()
        conn.close()
        print(f'  → {len(tse_d_rows)} 筆，{len(tse_d_stocks)} 支股票')
    elif '--tse' in args:
        # 只跑 TSE 注意股、處置股、歷史股價（快速 debug 用）
        init_db()
        conn    = get_db()
        now_str = datetime.now().isoformat()
        tse_codes = set()

        # TSE 注意股
        print('\n[TSE] 注意股（近30日）...')
        tse_n_stocks, tse_n_rows = [], []
        for row in fetch_tse_notice(days=30):
            if len(row) < 6:
                continue
            code = str(row[1]).strip()
            if len(code) != 4 or not code.isdigit():
                continue
            name      = str(row[2]).strip()
            date      = roc_slash_to_iso(row[5])
            if not date:
                continue
            reason    = str(row[4]).strip() if len(row) > 4 else ''
            close_val = safe_float(row[6]) if len(row) > 6 else 0.0
            tse_codes.add(code)
            tse_n_stocks.append((code, name, 'TSE', now_str))
            tse_n_rows.append((code, name, 'TSE', date, reason, close_val))
        if tse_n_stocks:
            conn.executemany('INSERT OR REPLACE INTO stocks (code,name,market,updated) VALUES (?,?,?,?)', tse_n_stocks)
        if tse_n_rows:
            conn.executemany('INSERT OR REPLACE INTO notice_stocks (code,name,market,date,reason,close) VALUES (?,?,?,?,?,?)', tse_n_rows)
        conn.commit()
        print(f'  → {len(tse_n_rows)} 筆，{len(tse_codes)} 支股票')

        # TSE 處置股
        time.sleep(2)
        print('\n[TSE] 處置股...')
        conn.execute("DELETE FROM disposition_stocks WHERE market='TSE'")
        conn.commit()
        tse_disp_codes = set()
        tse_d_stocks, tse_d_rows = [], []
        for row in fetch_tse_disposal():
            if len(row) < 4:
                continue
            code = str(row[2]).strip()
            name = str(row[3]).strip()
            if not code:
                continue
            announce = roc_slash_to_iso(row[1]) if len(row) > 1 else ''
            period   = str(row[6]) if len(row) > 6 else ''
            sd = ed = ''
            if '～' in period or '~' in period:
                parts = period.replace('~', '～').split('～')
                sd = roc_slash_to_iso(parts[0])
                ed = roc_slash_to_iso(parts[1]) if len(parts) > 1 else ''
            tse_disp_codes.add(code)
            tse_codes.add(code)
            tse_d_stocks.append((code, name, 'TSE', now_str))
            tse_d_rows.append((code, name, 'TSE', announce, sd, ed,
                               str(row[5]) if len(row) > 5 else '',
                               str(row[7]) if len(row) > 7 else '',
                               str(row[8]) if len(row) > 8 else ''))
        if tse_d_stocks:
            conn.executemany('INSERT OR REPLACE INTO stocks (code,name,market,updated) VALUES (?,?,?,?)', tse_d_stocks)
        if tse_d_rows:
            conn.executemany('INSERT OR REPLACE INTO disposition_stocks (code,name,market,announce_date,start_date,end_date,reason,measure,content) VALUES (?,?,?,?,?,?,?,?,?)', tse_d_rows)
        conn.commit()
        print(f'  → {len(tse_d_rows)} 筆，{len(tse_disp_codes)} 支股票')

        # TSE 歷史股價（近 35 天）
        print('\n[TSE] 歷史股價（近35日）...')
        today = datetime.now().date()
        tse_all, tse_names = {}, {}
        t_days = 0
        for offset in range(90):
            target = today - timedelta(days=offset)
            if target.weekday() >= 5:
                continue
            iso = target.strftime('%Y-%m-%d')
            day_data = fetch_tse_daily_all(iso)
            if day_data:
                t_days += 1
                for code, data in day_data.items():
                    tse_all.setdefault(code, []).append(data)
                    if data.get('name') and code not in tse_names:
                        tse_names[code] = data['name']
            time.sleep(0.3)
        print(f'  抓到 {t_days} 個交易日，{len(tse_all)} 支股票')

        name_rows = [(code, name, 'TSE', now_str) for code, name in tse_names.items() if name]
        if name_rows:
            conn.executemany('INSERT OR REPLACE INTO stocks (code,name,market,updated) VALUES (?,?,?,?)', name_rows)
            conn.commit()

        n = bulk_insert_prices(conn, tse_all, 'TSE')
        conn.commit()
        conn.close()
        print(f'  → 寫入 {n} 筆股價')
    elif '--prices-only' in args:
        init_db()
        update_prices_only()
    elif '--test-price' in args:
        # 快速測試：插入一筆假股價，驗證 executemany 是否正常
        init_db()
        conn = get_db()
        test_rows = [('2330', 'TSE', '2026-05-29', 980.0, 995.0, 975.0, 985.0, 50000, 5.0)]
        conn.executemany(
            'INSERT OR REPLACE INTO daily_price '
            '(code,market,date,open,high,low,close,volume,change) VALUES (?,?,?,?,?,?,?,?,?)',
            test_rows
        )
        conn.commit()
        conn.close()
        print('測試插入成功！executemany 正常運作。')
    elif '--otc' in args:
        # 只跑 OTC 注意股、處置股、歷史股價（快速 debug 用）
        init_db()
        conn   = get_db()
        now_str = datetime.now().isoformat()
        otc_codes = set()

        # OTC 注意股
        print('\n[OTC] 注意股（近30日）...')
        otc_n_stocks, otc_n_rows = [], []
        for row in fetch_otc_notice(days=30):
            if len(row) < 7:
                continue
            code = str(row[1]).strip()
            if len(code) != 4 or not code.isdigit():
                continue
            name      = str(row[2]).strip()
            date      = roc_slash_to_iso(row[5])
            if not date:
                continue
            reason    = str(row[4]).strip()
            close_val = safe_float(row[6])
            otc_codes.add(code)
            otc_n_stocks.append((code, name, 'OTC', now_str))
            otc_n_rows.append((code, name, 'OTC', date, reason, close_val))
        if otc_n_stocks:
            conn.executemany('INSERT OR REPLACE INTO stocks (code,name,market,updated) VALUES (?,?,?,?)', otc_n_stocks)
        if otc_n_rows:
            conn.executemany('INSERT OR REPLACE INTO notice_stocks (code,name,market,date,reason,close) VALUES (?,?,?,?,?,?)', otc_n_rows)
        conn.commit()
        print(f'  → {len(otc_n_rows)} 筆，{len(otc_codes)} 支股票')

        # OTC 處置股
        print('\n[OTC] 處置股...')
        conn.execute("DELETE FROM disposition_stocks WHERE market='OTC'")
        conn.commit()
        otc_d_stocks, otc_d_rows = [], []
        for item in fetch_otc_disposal():
            code = str(item.get('SecuritiesCompanyCode', '')).strip()
            name = str(item.get('CompanyName', '')).strip()
            if not code:
                continue
            announce = roc8_to_iso(item.get('Date', ''))
            period   = str(item.get('DispositionPeriod', ''))
            sd = ed = ''
            if '~' in period:
                parts = period.split('~')
                sd = roc8_to_iso(parts[0])
                ed = roc8_to_iso(parts[1]) if len(parts) > 1 else ''
            otc_codes.add(code)
            otc_d_stocks.append((code, name, 'OTC', now_str))
            otc_d_rows.append((code, name, 'OTC', announce, sd, ed,
                               str(item.get('DispositionReasons', '')),
                               str(item.get('DispositionMeasure', '')),
                               str(item.get('DisposalCondition', ''))))
        if otc_d_stocks:
            conn.executemany('INSERT OR REPLACE INTO stocks (code,name,market,updated) VALUES (?,?,?,?)', otc_d_stocks)
        if otc_d_rows:
            conn.executemany('INSERT OR REPLACE INTO disposition_stocks (code,name,market,announce_date,start_date,end_date,reason,measure,content) VALUES (?,?,?,?,?,?,?,?,?)', otc_d_rows)
        conn.commit()
        print(f'  → {len(otc_d_rows)} 筆，{len(otc_d_stocks)} 支股票')

        # OTC 歷史股價（近 35 天）
        print('\n[OTC] 歷史股價（近35日）...')
        today = datetime.now().date()
        otc_all, otc_names = {}, {}
        t_days = 0
        for offset in range(90):
            target = today - timedelta(days=offset)
            if target.weekday() >= 5:
                continue
            iso = target.strftime('%Y-%m-%d')
            day_data = fetch_otc_daily_all(iso)
            if day_data:
                t_days += 1
                for code, data in day_data.items():
                    otc_all.setdefault(code, []).append(data)
                    if data.get('name') and code not in otc_names:
                        otc_names[code] = data['name']
            time.sleep(0.3)
        print(f'  抓到 {t_days} 個交易日，{len(otc_all)} 支股票')

        # 寫入名稱
        name_rows = [(code, name, 'OTC', now_str) for code, name in otc_names.items() if name]
        if name_rows:
            conn.executemany('INSERT OR REPLACE INTO stocks (code,name,market,updated) VALUES (?,?,?,?)', name_rows)
            conn.commit()

        # 批次寫入股價
        n = bulk_insert_prices(conn, otc_all, 'OTC')
        conn.commit()
        conn.close()
        print(f'  → 寫入 {n} 筆股價')
    else:
        init_db()
        update_all()
