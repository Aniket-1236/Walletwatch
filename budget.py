"""Keeps WalletWatch inside a monthly request budget (Helius free plan = 1,000,000 credits).

Every HTTP call to the budgeted host costs 1 credit (getTransaction, getSignaturesForAddress ...).
Websocket data costs 2 credits per 0.1 MB. The counter is saved in the database, so restarting the
app never resets it, and it follows the billing month (set HELIUS_CYCLE_DAY if yours is not the 1st).

Modes:  normal -> lean (on pace to overspend: fewer background checks, no extra verification calls)
        -> paused (budget reserve reached: no more calls until the next cycle, so Helius never has to refuse us)."""
import datetime as dt
import threading
import time

from rpc import RpcError


class BudgetExceeded(RpcError):
    pass


def _utc(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc)


class Budget:
    def __init__(self, db, limit=1_000_000, reserve=0.08, cycle_day=1, enabled=True, hosts=None, clock=time.time):
        self.db, self.limit, self.reserve = db, int(limit), min(0.5, max(0.0, reserve))
        self.cycle_day = min(28, max(1, int(cycle_day)))
        self.enabled, self.clock = enabled, clock
        self.hosts = tuple(hosts) if hosts else ("helius-rpc.com",)
        self.lock = threading.Lock()
        self._unsaved, self._last_save = 0.0, 0.0
        self._pkey = self._dkey = None
        self._used = self._today = 0.0
        self._load(clock())

    # ---- which requests are counted
    def counts(self, host):
        return self.enabled and any(host == h or host.endswith("." + h) or host.endswith(h) for h in self.hosts)

    # ---- billing period
    def period(self, now=None):
        d = _utc(now or self.clock())
        y, m = (d.year, d.month) if d.day >= self.cycle_day else ((d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12))
        start = dt.datetime(y, m, self.cycle_day, tzinfo=dt.timezone.utc)
        ny, nm = (y, m + 1) if m < 12 else (y + 1, 1)
        end = dt.datetime(ny, nm, self.cycle_day, tzinfo=dt.timezone.utc)
        return start, end

    def _keys(self, now):
        return "credits:" + self.period(now)[0].strftime("%Y-%m-%d"), "credits_day:" + _utc(now).strftime("%Y-%m-%d")

    def _load(self, now):
        self._pkey, self._dkey = self._keys(now)
        self._used = float(self.db.get_setting(self._pkey, 0) or 0)
        self._today = float(self.db.get_setting(self._dkey, 0) or 0)

    def _roll(self, now):
        pk, dk = self._keys(now)
        if (pk, dk) != (self._pkey, self._dkey):
            self._save()
            self._load(now)

    def _save(self):
        self.db.set_setting(self._pkey, round(self._used, 3))
        self.db.set_setting(self._dkey, round(self._today, 3))
        self._unsaved, self._last_save = 0.0, self.clock()

    # ---- counting
    def add(self, credits=1.0):
        if not self.enabled:
            return
        with self.lock:
            now = self.clock()
            self._roll(now)
            self._used += credits
            self._today += credits
            self._unsaved += credits
            if self._unsaved >= 25 or now - self._last_save >= 10:
                self._save()

    def add_ws_bytes(self, nbytes):
        self.add(nbytes / 100_000.0 * 2.0)       # 2 credits per 0.1 MB streamed

    def flush(self):
        with self.lock:
            self._save()

    # ---- numbers
    def usable(self):
        return self.limit * (1 - self.reserve)

    def used(self):
        with self.lock:
            self._roll(self.clock())
            return self._used

    def today_used(self):
        with self.lock:
            self._roll(self.clock())
            return self._today

    def daily_allowance(self, now=None):
        """Credits we may spend per day from now to the end of the cycle (today included)."""
        now = now or self.clock()
        with self.lock:
            self._roll(now)
            before_today = self._used - self._today
        _, end = self.period(now)
        days_left = max(1.0, (end - _utc(now)).total_seconds() / 86400.0)
        return max(0.0, (self.usable() - before_today) / days_left)

    def projected(self, now=None):
        now = now or self.clock()
        start, end = self.period(now)
        elapsed = max(0.5, (_utc(now) - start).total_seconds() / 86400.0)    # avoid wild guesses on day 1
        return self.used() / elapsed * (end - start).total_seconds() / 86400.0

    def mode(self, now=None):
        if not self.enabled:
            return "off"
        if self.used() >= self.usable():
            return "paused"
        now = now or self.clock()
        start, _ = self.period(now)
        days_in = (_utc(now) - start).total_seconds() / 86400.0
        if (days_in >= 1 and self.projected(now) > self.usable()) or self.today_used() > self.daily_allowance(now) * 1.5:
            return "lean"
        return "normal"

    def check(self):
        if self.enabled and self.used() >= self.usable():
            _, end = self.period()
            raise BudgetExceeded(f"Monthly request budget reached ({int(self.used()):,} of {self.limit:,}). "
                                 f"Checks resume on {end.strftime('%d %b')} so you are never charged or blocked.")

    def snapshot(self):
        if not self.enabled:
            return {"enabled": False}
        now = self.clock()
        start, end = self.period(now)
        return {"enabled": True, "mode": self.mode(now), "limit": self.limit, "usable": int(self.usable()),
                "used": int(self.used()), "today": int(self.today_used()),
                "daily_budget": int(self.daily_allowance(now)), "projected": int(self.projected(now)),
                "period_start": start.timestamp(), "period_end": end.timestamp()}
