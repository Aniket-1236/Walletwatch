"""Login for the website: password (and optional authenticator code) checked ONLY on the server.

* The password is never sent to the browser, never in the page, never in a URL. The page only contains a form.
* It is stored as a slow salted hash (PBKDF2-SHA256, 600,000 rounds) in .env, not as plain text.
* After a correct login the server gives the browser a random 256-bit session cookie (HttpOnly, SameSite=Strict,
  Secure on https). Only a hash of that cookie is kept in the database.
* Wrong attempts are counted per IP address; 5 wrong tries lock that address out for 15 minutes.
* Optional second step: a 6-digit code from an authenticator app (TOTP, RFC 6238)."""
import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import time

ITERATIONS = 600_000
MAX_FAILS, LOCK_SECONDS = 5, 15 * 60
MIN_LENGTH = 10
FAIL_DELAY = 0.4          # seconds every wrong attempt waits (slows guessing)
COMMON = {"password", "1234567890", "qwertyuiop", "iloveyou12", "walletwatch", "password123", "0123456789", "admin12345"}


def _b64(b):
    return base64.b64encode(b).decode()


def hash_password(password, iterations=ITERATIONS, salt=None):
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(dk)}"


def check_hash(password, stored):
    try:
        scheme, iters, salt, want = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


def password_problem(pw):
    if len(pw) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters (a long phrase is best)."
    if pw.lower() in COMMON or len(set(pw)) < 4:
        return "That password is too easy to guess."
    return None


# ---------- authenticator codes (TOTP, RFC 6238: HMAC-SHA1, 30 s steps, 6 digits)
def new_totp_secret():
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def totp_code(secret, counter, digits=6):
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    return str((struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)).zfill(digits)


def otpauth_uri(secret, name="owner", issuer="WalletWatch"):
    return f"otpauth://totp/{issuer}:{name}?secret={secret}&issuer={issuer}"


def write_env(path, updates, remove=()):
    """Set KEY=VALUE lines in a .env file (creating it) and drop the keys in `remove`."""
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            lines = [l.rstrip("\n") for l in f]
    keys = set(updates) | set(remove)
    lines = [l for l in lines if l.split("=", 1)[0].strip() not in keys]
    lines += [f"{k}={v}" for k, v in updates.items()]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)           # only you can read it (Linux/Mac)
    except OSError:
        pass


class Auth:
    def __init__(self, db, clock=time.time):
        self.db, self.clock = db, clock
        self._plain_salt = os.urandom(16)

    # ---- configuration (read live from the environment)
    @staticmethod
    def _hash():
        return os.getenv("APP_PASSWORD_HASH", "").strip()

    @staticmethod
    def _plain():
        return os.getenv("APP_PASSWORD", "")

    @staticmethod
    def totp_secret():
        return re.sub(r"\s+", "", os.getenv("TOTP_SECRET", "")).upper()

    def configured(self):
        return bool(self._hash() or self._plain())

    def mode(self):
        """'login' (password set), 'open' (no password, allowed for demo/local), 'locked' (no password: refuse)."""
        if self.configured():
            return "login"
        if os.getenv("ALLOW_NO_PASSWORD") == "1" or os.getenv("MOCK") == "1":
            return "open"
        return "locked"

    def totp_enabled(self):
        return bool(self.totp_secret())

    def _fingerprint(self):
        """Changes when the password or the authenticator secret changes, which ends every old session."""
        return hashlib.sha256(("fp|" + (self._hash() or self._plain()) + "|" + self.totp_secret()).encode()).hexdigest()[:24]

    # ---- checks
    def check_password(self, given):
        stored = self._hash()
        if stored:
            return check_hash(given, stored)
        plain = self._plain()
        a = hashlib.sha256(self._plain_salt + given.encode()).digest()
        b = hashlib.sha256(self._plain_salt + plain.encode()).digest()
        return bool(plain) and hmac.compare_digest(a, b)

    def check_totp(self, code):
        secret = self.totp_secret()
        code = re.sub(r"\s+", "", code or "")
        if not secret or not re.fullmatch(r"\d{6}", code):
            return False
        now_step = int(self.clock() // 30)
        last = int(float(self.db.get_setting("totp_last", 0) or 0))
        for step in (now_step - 1, now_step, now_step + 1):
            if step > last and hmac.compare_digest(totp_code(secret, step), code):
                self.db.set_setting("totp_last", step)        # a code works only once
                return True
        return False

    # ---- login / sessions
    def locked_for(self, ip):
        now = self.clock()
        fails = self.db.attempts_since(ip, now - LOCK_SECONDS)
        if len(fails) >= MAX_FAILS:
            return max(1, min(LOCK_SECONDS, int(fails[-MAX_FAILS] + LOCK_SECONDS - now) + 1))
        return 0

    def login(self, ip, password, code=None):
        """Returns dict(ok, token, status, error, retry_after)."""
        wait = self.locked_for(ip)
        if wait:
            return {"ok": False, "status": 429, "retry_after": wait,
                    "error": f"Too many wrong attempts. Try again in {max(1, round(wait / 60))} min."}
        good = self.check_password(password or "")
        if good and self.totp_enabled():
            good = self.check_totp(code)
        if not good:
            self.db.add_attempt(ip, self.clock())
            self.db.purge_attempts(self.clock() - 86400)
            time.sleep(FAIL_DELAY)                           # slows down guessing even before the lockout
            return {"ok": False, "status": 401, "error": "Wrong password or code."}
        self.db.clear_attempts(ip)
        now = self.clock()
        self.db.purge_sessions(now, self.idle_seconds())
        token = secrets.token_urlsafe(32)
        self.db.add_session(self._th(token), self._fingerprint(), now, now + self.max_seconds(), ip)
        return {"ok": True, "status": 200, "token": token, "max_age": int(self.max_seconds())}

    @staticmethod
    def _th(token):
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def max_seconds():
        return float(os.getenv("SESSION_DAYS", "7")) * 86400

    @staticmethod
    def idle_seconds():
        return float(os.getenv("SESSION_IDLE_HOURS", "12")) * 3600

    def valid(self, token):
        if not token or len(token) > 100:
            return False
        s = self.db.get_session(self._th(token))
        now = self.clock()
        if not s or s["expires"] < now or now - s["last_seen"] > self.idle_seconds() \
                or not hmac.compare_digest(s["fp"], self._fingerprint()):
            if s:
                self.db.del_session(s["th"])
            return False
        if now - s["last_seen"] > 60:
            self.db.touch_session(s["th"], now)
        return True

    def logout(self, token):
        if token:
            self.db.del_session(self._th(token))
