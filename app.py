"""
台股注意股・處置股查詢系統 — Flask Web 伺服器
==============================================
本機開發：python app.py
雲端部署：上傳至 Render / Railway，啟動指令 gunicorn app:app
"""

import os
import requests
from flask import Flask, send_from_directory, request, Response, jsonify

app = Flask(__name__, static_folder='.')

# TWSE API 對應表
TWSE_MAP = {
    'stock_day': 'https://www.twse.com.tw/exchangeReport/STOCK_DAY',
    'notice':    'https://www.twse.com.tw/announcement/notice',
    'punish':    'https://www.twse.com.tw/announcement/punish',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Referer': 'https://www.twse.com.tw/',
    'Accept':  'application/json, text/plain, */*',
}


# ── 靜態頁面 ────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('.', '注意股處置股查詢系統.html')


# ── TWSE API 代理 ───────────────────────────────────
@app.route('/proxy/<name>')
def proxy(name):
    if name not in TWSE_MAP:
        return jsonify(stat='ERROR', message=f'未知端點：{name}'), 404

    url    = TWSE_MAP[name]
    params = dict(request.args)      # 直接轉發所有 query params

    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return Response(
            resp.content,
            status=200,
            content_type='application/json;charset=UTF-8',
            headers={'Access-Control-Allow-Origin': '*'}
        )
    except requests.exceptions.Timeout:
        return jsonify(stat='ERROR', message='TWSE 回應逾時，請稍後再試'), 504
    except requests.exceptions.RequestException as e:
        return jsonify(stat='ERROR', message=str(e)), 502


# ── 啟動 ────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8866))
    print(f'✅ 伺服器已啟動：http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=False)
