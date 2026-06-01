"""
cloud_db.py — Turso HTTP API / SQLite 共用資料庫適配器

只需 requests（已內建），不需要安裝 libsql-experimental。

環境變數切換：
  TURSO_DATABASE_URL = libsql://your-db.turso.io  （或 https://...）
  TURSO_AUTH_TOKEN   = your-token

未設定時自動使用本機 SQLite。
"""
import os
import requests as _req

TURSO_URL   = os.environ.get('TURSO_DATABASE_URL', '').strip()
TURSO_TOKEN = os.environ.get('TURSO_AUTH_TOKEN',   '').strip()


# ── Turso 型別轉換 ────────────────────────────────

import math as _math

def _to_arg(v):
    """
    Python 值 → Turso HTTP API 參數格式（hrana 協議）
    integer → value 為字串（保留 64-bit 精度）
    float   → value 為 JSON 數字
    text    → value 為字串
    """
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": "1" if v else "0"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}   # 字串
    if isinstance(v, float):
        if _math.isnan(v) or _math.isinf(v):
            return {"type": "null"}
        return {"type": "float", "value": v}           # 數字
    return {"type": "text", "value": str(v)}


def _from_cell(cell):
    """Turso HTTP API 回傳值 → Python 值"""
    if cell is None:
        return None
    if isinstance(cell, dict):
        t, v = cell.get("type", "text"), cell.get("value")
        if t == "null" or v is None:
            return None
        if t == "integer":
            return int(v)
        if t == "float":
            return float(v)
        return v
    return cell  # 已經是 Python 原生型別


# ── 通用結果列（相容 sqlite3.Row）────────────────────

class _Row(dict):
    """同時支援 row['col'] 和 row[0] 的結果列"""
    def __init__(self, description, values):
        super().__init__(zip((d[0] for d in description), values))
        self._v = list(values)

    def __getitem__(self, key):
        return self._v[key] if isinstance(key, int) else super().__getitem__(key)


# ── Turso 遊標 ────────────────────────────────────

class _TursoCursor:
    def __init__(self, cols, raw_rows, affected=0):
        self._desc  = [(c, None, None, None, None, None, None) for c in cols]
        self._rows  = [[_from_cell(cell) for cell in row] for row in raw_rows]
        self._pos   = 0
        self._rowcount = affected

    def _wrap(self, values):
        return _Row(self._desc, values)

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._wrap(self._rows[self._pos])
        self._pos += 1
        return row

    def fetchall(self):
        rows = [self._wrap(r) for r in self._rows[self._pos:]]
        self._pos = len(self._rows)
        return rows

    @property
    def description(self):
        return self._desc

    @property
    def rowcount(self):
        return self._rowcount


# ── Turso HTTP 連線（相容 sqlite3 介面）──────────────

class _TursoConn:
    BATCH = 200  # 每批最多送幾條 SQL（executemany 用）

    def __init__(self, url, token):
        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]
        self._base  = url.rstrip('/')
        self._token = token

    def _pipeline(self, stmts):
        """
        送出一批 SQL，回傳 results 清單。
        stmts: [(sql, [args]), ...]
        """
        body = [
            {"type": "execute",
             "stmt": {"sql": sql,
                      "args": [_to_arg(a) for a in args]}}
            for sql, args in stmts
        ]
        body.append({"type": "close"})
        resp = _req.post(
            f"{self._base}/v2/pipeline",
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json"},
            json={"requests": body},
            timeout=20
        )
        if not resp.ok:
            raise Exception(
                f'Turso HTTP {resp.status_code}：{resp.text[:400]}'
            )
        return resp.json()["results"]

    def execute(self, sql, params=()):
        results = self._pipeline([(sql, list(params))])
        r = results[0]
        if r.get("type") == "error":
            raise Exception(f'Turso SQL 錯誤：{r.get("error",{}).get("message","?")}')
        res     = r["response"]["result"]
        cols    = [c["name"] for c in res.get("cols", [])]
        return _TursoCursor(cols, res.get("rows", []),
                            res.get("affected_row_count", 0))

    def executemany(self, sql, params_list):
        """批次執行，每 BATCH 條打包一次 HTTP 請求（BEGIN/COMMIT 包事務）"""
        rows = list(params_list)
        for i in range(0, len(rows), self.BATCH):
            chunk = rows[i:i + self.BATCH]
            stmts = (
                [("BEGIN", [])] +
                [(sql, list(p)) for p in chunk] +
                [("COMMIT", [])]
            )
            self._pipeline(stmts)

    def executescript(self, script):
        """將多條 SQL 拆開逐一執行（init_db 用）"""
        stmts = [(s.strip(), [])
                 for s in script.split(';')
                 if s.strip()]
        if not stmts:
            return
        results = self._pipeline(stmts)
        for i, r in enumerate(results[:-1]):  # 跳過最後的 close 回應
            if r.get("type") == "error":
                err  = r.get("error", {}).get("message", "?")
                hint = stmts[i][0][:100] if i < len(stmts) else "?"
                raise Exception(
                    f'Turso executescript 第 {i+1} 條語句失敗：{err}\n'
                    f'SQL：{hint}'
                )

    def commit(self):
        pass  # HTTP API 每條語句自動 commit

    def close(self):
        pass  # HTTP 無狀態，不需要關閉

    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, _):
        pass  # 由 _TursoCursor 處理，忽略此設定

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ── 公開介面 ──────────────────────────────────────

def open_db(local_path: str = None, *, pragma_wal: bool = False):
    """回傳 Turso 雲端資料庫連線（需設定 TURSO_DATABASE_URL / TURSO_AUTH_TOKEN）"""
    if not TURSO_URL or not TURSO_TOKEN:
        raise RuntimeError(
            '請在 .env 設定 TURSO_DATABASE_URL 與 TURSO_AUTH_TOKEN'
        )
    return _TursoConn(TURSO_URL, TURSO_TOKEN)


def using_turso() -> bool:
    return True
