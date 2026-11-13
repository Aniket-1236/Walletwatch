import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chain TEXT NOT NULL, address TEXT NOT NULL,
  label TEXT NOT NULL, added_at REAL NOT NULL, UNIQUE(chain, address));
CREATE TABLE IF NOT EXISTS buys(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  wallet_id INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
  chain TEXT NOT NULL, token TEXT NOT NULL, symbol TEXT, amount REAL,
  tx TEXT NOT NULL, ts REAL NOT NULL, UNIQUE(wallet_id, tx, token));
CREATE INDEX IF NOT EXISTS buys_ts ON buys(ts);
CREATE TABLE IF NOT EXISTS fbuys(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  wallet_id INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
  chain TEXT NOT NULL, token TEXT NOT NULL, symbol TEXT, amount REAL,
  tx TEXT NOT NULL, ts REAL NOT NULL, detected_at REAL NOT NULL,
  first_ever INTEGER NOT NULL DEFAULT 1, UNIQUE(wallet_id, token));
CREATE INDEX IF NOT EXISTS fbuys_ts ON fbuys(ts);
CREATE INDEX IF NOT EXISTS fbuys_token ON fbuys(chain, token);
CREATE TABLE IF NOT EXISTS sessions(
  th TEXT PRIMARY KEY, fp TEXT NOT NULL, created REAL NOT NULL, last_seen REAL NOT NULL,
  expires REAL NOT NULL, ip TEXT);
CREATE TABLE IF NOT EXISTS login_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, ip TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS la_ip ON login_attempts(ip, ts);
CREATE TABLE IF NOT EXISTS cursors(
  wallet_id INTEGER PRIMARY KEY REFERENCES wallets(id) ON DELETE CASCADE, value TEXT);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chain TEXT, token TEXT, symbol TEXT,
  wallet_count INTEGER, score INTEGER, message TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""


class DB:
    def __init__(self, path):
        self.lock = threading.RLock()
        folder = os.path.dirname(path)
        if path != ":memory:" and folder:
            os.makedirs(folder, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA foreign_keys=ON")
            try:   # DB_JOURNAL=DELETE is safer when two machines share the file (PythonAnywhere)
                mode = os.getenv("DB_JOURNAL", "WAL").upper()
                self.conn.execute("PRAGMA journal_mode=" + (mode if mode in ("WAL", "DELETE", "TRUNCATE") else "WAL"))
            except sqlite3.DatabaseError:
                pass
            self.conn.executescript(SCHEMA)
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(buys)").fetchall()]
            if "detected_at" not in cols:   # older databases: add the column used by the All Buys tab
                self.conn.execute("ALTER TABLE buys ADD COLUMN detected_at REAL")
            self.conn.commit()

    def _all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _run(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    # wallets
    def add_wallet(self, chain, address, label):
        try:
            cur = self._run("INSERT INTO wallets(chain,address,label,added_at) VALUES(?,?,?,?)",
                            (chain, address, label, time.time()))
        except sqlite3.IntegrityError:
            raise ValueError("That wallet is already on this chain in your list.")
        return cur.lastrowid

    def remove_wallet(self, wallet_id):
        return self._run("DELETE FROM wallets WHERE id=?", (wallet_id,)).rowcount > 0

    def list_wallets(self):
        return self._all("SELECT * FROM wallets ORDER BY id")

    def get_wallet(self, wallet_id):
        rows = self._all("SELECT * FROM wallets WHERE id=?", (wallet_id,))
        return rows[0] if rows else None

    # cursors
    def get_cursor(self, wallet_id):
        rows = self._all("SELECT value FROM cursors WHERE wallet_id=?", (wallet_id,))
        return rows[0]["value"] if rows else None

    def set_cursor(self, wallet_id, value):
        if value is None:
            return
        self._run("INSERT INTO cursors(wallet_id,value) VALUES(?,?) "
                  "ON CONFLICT(wallet_id) DO UPDATE SET value=excluded.value", (wallet_id, str(value)))

    # login sessions (only a SHA-256 of the cookie token is stored, never the token itself) and failed attempts
    def add_session(self, th, fp, now, expires, ip):
        self._run("INSERT OR REPLACE INTO sessions(th,fp,created,last_seen,expires,ip) VALUES(?,?,?,?,?,?)",
                  (th, fp, now, now, expires, ip))

    def get_session(self, th):
        rows = self._all("SELECT * FROM sessions WHERE th=?", (th,))
        return rows[0] if rows else None

    def touch_session(self, th, now):
        self._run("UPDATE sessions SET last_seen=? WHERE th=?", (now, th))

    def del_session(self, th):
        self._run("DELETE FROM sessions WHERE th=?", (th,))

    def purge_sessions(self, now, idle_seconds, keep=20):
        self._run("DELETE FROM sessions WHERE expires<? OR last_seen<?", (now, now - idle_seconds))
        self._run("DELETE FROM sessions WHERE th NOT IN (SELECT th FROM sessions ORDER BY created DESC LIMIT ?)", (keep,))

    def clear_sessions(self):
        self._run("DELETE FROM sessions")

    def add_attempt(self, ip, ts):
        self._run("INSERT INTO login_attempts(ip,ts) VALUES(?,?)", (ip, ts))

    def attempts_since(self, ip, since):
        return [r["ts"] for r in self._all("SELECT ts FROM login_attempts WHERE ip=? AND ts>=? ORDER BY ts", (ip, since))]

    def clear_attempts(self, ip=None):
        if ip is None:
            self._run("DELETE FROM login_attempts")
        else:
            self._run("DELETE FROM login_attempts WHERE ip=?", (ip,))

    def purge_attempts(self, before):
        self._run("DELETE FROM login_attempts WHERE ts<?", (before,))

    # buys: ONE row per (wallet, token). A repeat buy of the same coin by the same wallet is ignored.
    # first_ever=0 rows only remember "this wallet already owned/traded this coin before we saw it".
    def add_buy(self, wallet_id, chain, token, symbol, amount, tx, ts, first_ever=True, detected_at=None):
        cur = self._run("INSERT OR IGNORE INTO fbuys(wallet_id,chain,token,symbol,amount,tx,ts,detected_at,first_ever) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (wallet_id, chain, token, symbol, amount, tx, ts, detected_at or time.time(),
                         1 if first_ever else 0))
        return cur.rowcount > 0

    # every buy transaction (including repeat buys of a coin) for the "All Buys" tab
    def add_tx_buy(self, wallet_id, chain, token, symbol, amount, tx, ts, detected_at=None):
        cur = self._run("INSERT OR IGNORE INTO buys(wallet_id,chain,token,symbol,amount,tx,ts,detected_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (wallet_id, chain, token, symbol, amount, tx, ts, detected_at or time.time()))
        return cur.rowcount > 0

    def latest_tx_buys(self, limit=60):
        return self._all(
            "SELECT b.id, b.wallet_id, b.chain, b.token, b.amount, b.tx, b.ts, b.detected_at, w.label AS wallet_label, "
            "COALESCE(b.symbol, (SELECT x.symbol FROM fbuys x WHERE x.chain=b.chain AND x.token=b.token "
            "AND x.symbol IS NOT NULL AND x.symbol<>'' LIMIT 1)) AS symbol, "
            "EXISTS(SELECT 1 FROM fbuys f WHERE f.wallet_id=b.wallet_id AND f.token=b.token AND f.tx=b.tx "
            "AND f.first_ever=1) AS is_first "
            "FROM buys b JOIN wallets w ON w.id=b.wallet_id ORDER BY b.ts DESC, b.id DESC LIMIT ?", (limit,))

    def has_buy(self, wallet_id, token):
        return bool(self._all("SELECT 1 FROM fbuys WHERE wallet_id=? AND token=?", (wallet_id, token)))

    def set_symbol(self, chain, token, symbol):
        if symbol:
            self._run("UPDATE fbuys SET symbol=? WHERE chain=? AND token=? AND (symbol IS NULL OR symbol='')",
                      (symbol, chain, token))
            self._run("UPDATE buys SET symbol=? WHERE chain=? AND token=? AND (symbol IS NULL OR symbol='')",
                      (symbol, chain, token))

    def buys_since(self, since):
        return self._all("SELECT b.*, w.label AS wallet_label FROM fbuys b JOIN wallets w ON w.id=b.wallet_id "
                         "WHERE b.first_ever=1 AND b.ts>=? ORDER BY b.ts DESC", (since,))

    def recent_buys(self, limit=40):
        return self._all("SELECT b.*, w.label AS wallet_label FROM fbuys b JOIN wallets w ON w.id=b.wallet_id "
                         "WHERE b.first_ever=1 ORDER BY b.ts DESC LIMIT ?", (limit,))

    def buy_counts(self):
        return {r["wallet_id"]: r["n"] for r in
                self._all("SELECT wallet_id, COUNT(*) AS n FROM fbuys WHERE first_ever=1 GROUP BY wallet_id")}

    # alerts
    def add_alert(self, chain, token, symbol, wallet_count, score, message, ts):
        return self._run("INSERT INTO alerts(chain,token,symbol,wallet_count,score,message,ts) "
                         "VALUES(?,?,?,?,?,?,?)",
                         (chain, token, symbol, wallet_count, score, message, ts)).lastrowid

    def last_alert(self, chain, token, since):
        rows = self._all("SELECT * FROM alerts WHERE chain=? AND token=? AND ts>=? "
                         "ORDER BY wallet_count DESC LIMIT 1", (chain, token, since))
        return rows[0] if rows else None

    def recent_alerts(self, limit=20):
        return self._all("SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,))

    # settings
    def get_setting(self, key, default=None):
        rows = self._all("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_setting(self, key, value):
        self._run("INSERT INTO kv(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
