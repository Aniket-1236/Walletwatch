"""Login security: password hashing, lockout, sessions, authenticator codes, cookies, CSRF, headers."""
import importlib, io, json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from wsgiref.util import setup_testing_defaults
import auth, db as dbmod

PW = "correct horse battery staple"
ENV = ("APP_PASSWORD", "APP_PASSWORD_HASH", "TOTP_SECRET", "ALLOW_NO_PASSWORD", "MOCK", "TRUST_PROXY", "COOKIE_SECURE",
       "DB_PATH", "SESSION_DAYS", "SESSION_IDLE_HOURS")
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"      # RFC 6238 test secret ("12345678901234567890")


class Clock:
    def __init__(self, t=1_800_000_000.0): self.t = t
    def __call__(self): return self.t


class EnvCase(unittest.TestCase):
    def setUp(self):
        self.keep = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)
        self.old_delay, auth.FAIL_DELAY = auth.FAIL_DELAY, 0
        self.old_iter, auth.ITERATIONS = auth.ITERATIONS, 1000          # fast hashing in tests only

    def tearDown(self):
        auth.FAIL_DELAY, auth.ITERATIONS = self.old_delay, self.old_iter
        for k, v in self.keep.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


class Primitives(EnvCase):
    def test_hash_is_salted_slow_and_verifies(self):
        h1, h2 = auth.hash_password(PW, iterations=1000), auth.hash_password(PW, iterations=1000)
        self.assertNotEqual(h1, h2)                                        # new random salt every time
        self.assertTrue(h1.startswith("pbkdf2_sha256$"))
        self.assertTrue(auth.check_hash(PW, h1)); self.assertFalse(auth.check_hash(PW + "x", h1))
        for junk in ("", "nonsense", "a$b$c$d", h1[:-4], h1.replace("pbkdf2_sha256", "md5")):
            self.assertFalse(auth.check_hash(PW, junk))                    # broken hashes never crash or pass
        self.assertEqual(auth.ITERATIONS, 1000)                            # (test override)
        self.assertEqual(importlib.reload(auth).ITERATIONS, 600_000)       # real default is slow on purpose

    def test_password_rules(self):
        self.assertIn("at least", auth.password_problem("short"))
        self.assertIn("easy", auth.password_problem("password123"))
        self.assertIn("easy", auth.password_problem("aaaaaaaaaaaa"))
        self.assertIsNone(auth.password_problem(PW))

    def test_totp_matches_rfc6238_and_codes_work_once(self):
        self.assertEqual(auth.totp_code(RFC_SECRET, 59 // 30), "287082")        # official RFC 6238 test value
        d, clk = dbmod.DB(":memory:"), Clock(59)
        os.environ["TOTP_SECRET"] = RFC_SECRET
        a = auth.Auth(d, clk)
        self.assertFalse(a.check_totp("000000")); self.assertFalse(a.check_totp("abcdef")); self.assertFalse(a.check_totp(""))
        self.assertTrue(a.check_totp("287 082"))                               # spaces are fine
        self.assertFalse(a.check_totp("287082"))                               # the same code cannot be used twice
        clk.t = 59 + 30
        self.assertTrue(a.check_totp(auth.totp_code(RFC_SECRET, int(clk.t // 30))))
        clk.t = 59 + 30 + 300                                                   # 5 minutes later: old window is dead
        self.assertFalse(a.check_totp(auth.totp_code(RFC_SECRET, int(clk.t // 30) - 5)))

    def test_mode(self):
        a = auth.Auth(dbmod.DB(":memory:"))
        self.assertEqual(a.mode(), "locked")                                   # no password: refuse, do not run open
        os.environ["MOCK"] = "1"; self.assertEqual(a.mode(), "open")
        os.environ.pop("MOCK"); os.environ["ALLOW_NO_PASSWORD"] = "1"; self.assertEqual(a.mode(), "open")
        os.environ["APP_PASSWORD"] = "x" * 12; self.assertEqual(a.mode(), "login")   # a password always wins


class LoginAndSessions(EnvCase):
    def setUp(self):
        super().setUp()
        os.environ["APP_PASSWORD_HASH"] = auth.hash_password(PW)
        self.db, self.clk = dbmod.DB(":memory:"), Clock()
        self.a = auth.Auth(self.db, self.clk)

    def test_wrong_then_right_password(self):
        self.assertEqual(self.a.login("1.1.1.1", "nope")["status"], 401)
        self.assertEqual(self.a.login("1.1.1.1", "")["status"], 401)
        r = self.a.login("1.1.1.1", PW)
        self.assertTrue(r["ok"] and len(r["token"]) >= 40)
        self.assertTrue(self.a.valid(r["token"]))
        self.assertFalse(self.a.valid("")); self.assertFalse(self.a.valid("x" * 200)); self.assertFalse(self.a.valid("guess"))

    def test_token_is_random_and_only_its_hash_is_stored(self):
        t1, t2 = self.a.login("1.1.1.1", PW)["token"], self.a.login("1.1.1.1", PW)["token"]
        self.assertNotEqual(t1, t2)
        stored = json.dumps(self.db._all("SELECT * FROM sessions"))
        self.assertNotIn(t1, stored); self.assertNotIn(t2, stored)

    def test_five_wrong_attempts_lock_that_address_for_15_minutes(self):
        for _ in range(5):
            self.assertEqual(self.a.login("9.9.9.9", "bad")["status"], 401)
        r = self.a.login("9.9.9.9", PW)                                         # even the RIGHT password is refused now
        self.assertEqual((r["ok"], r["status"]), (False, 429)); self.assertLessEqual(r["retry_after"], 900)
        self.assertTrue(self.a.login("8.8.8.8", PW)["ok"])                       # other addresses are not affected
        self.clk.t += 15 * 60 + 5
        self.assertTrue(self.a.login("9.9.9.9", PW)["ok"])                       # lock ends by itself

    def test_success_resets_the_counter(self):
        for _ in range(4):
            self.a.login("7.7.7.7", "bad")
        self.assertTrue(self.a.login("7.7.7.7", PW)["ok"])
        for _ in range(4):
            self.assertEqual(self.a.login("7.7.7.7", "bad")["status"], 401)

    def test_session_expiry_idle_logout_and_password_change(self):
        t = self.a.login("1.1.1.1", PW)["token"]
        self.clk.t += 11 * 3600; self.assertTrue(self.a.valid(t))               # within idle limit
        self.clk.t += 13 * 3600; self.assertFalse(self.a.valid(t))              # 13 h idle: gone
        t = self.a.login("1.1.1.1", PW)["token"]
        self.a.logout(t); self.assertFalse(self.a.valid(t))
        t = self.a.login("1.1.1.1", PW)["token"]
        os.environ["APP_PASSWORD_HASH"] = auth.hash_password("a brand new long password")
        self.assertFalse(self.a.valid(t))                                       # changing the password logs everyone out
        os.environ["APP_PASSWORD_HASH"] = auth.hash_password(PW)
        t = self.a.login("1.1.1.1", PW)["token"]
        for _ in range(12):                                                      # keep it active for 8 days
            self.clk.t += 16 * 3600; self.a.valid(t)
        self.assertFalse(self.a.valid(t))                                       # absolute limit (7 days) still ends it

    def test_authenticator_code_is_required_when_enabled(self):
        os.environ["TOTP_SECRET"] = RFC_SECRET
        self.assertEqual(self.a.login("1.1.1.1", PW)["status"], 401)             # password alone is not enough
        self.assertEqual(self.a.login("1.1.1.1", PW, "123456")["status"], 401)
        code = auth.totp_code(RFC_SECRET, int(self.clk.t // 30))
        self.assertEqual(self.a.login("1.1.1.1", "wrong", code)["status"], 401)  # code alone is not enough
        self.assertTrue(self.a.login("1.1.1.1", PW, code)["ok"])

    def test_old_plain_app_password_still_works_but_wrong_one_fails(self):
        os.environ.pop("APP_PASSWORD_HASH"); os.environ["APP_PASSWORD"] = "legacy-password-1"
        a = auth.Auth(self.db, self.clk)
        self.assertTrue(a.login("1.1.1.1", "legacy-password-1")["ok"]); self.assertFalse(a.login("1.1.1.1", "legacy-password-2")["ok"])


class Web(EnvCase):
    """Real request handler through the WSGI adapter (same Handler class as python server.py)."""
    def setUp(self):
        super().setUp()
        os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "w.db")
        os.environ["APP_PASSWORD_HASH"] = auth.hash_password(PW)
        import wsgi_app
        self.web = importlib.reload(wsgi_app)

    def call(self, method, path, body=None, headers=None, https=False, ip="5.5.5.5"):
        env = {}
        setup_testing_defaults(env)
        raw = json.dumps(body).encode() if body is not None else b""
        env.update(REQUEST_METHOD=method, PATH_INFO=path, QUERY_STRING="", CONTENT_LENGTH=str(len(raw)), REMOTE_ADDR=ip,
                   CONTENT_TYPE="application/json", HTTP_HOST="panel.example", **{"wsgi.input": io.BytesIO(raw)})
        env["wsgi.url_scheme"] = "https" if https else "http"
        for k, v in (headers or {}).items():
            env["HTTP_" + k.upper().replace("-", "_")] = v
        out = {}
        res = self.web.application(env, lambda status, hdrs, exc=None: out.update(status=int(status[:3]), headers=hdrs))
        hdr = {}
        for k, v in out["headers"]:
            hdr.setdefault(k.lower(), []).append(v)
        return out["status"], hdr, b"".join(res)

    def login(self, pw=PW, **kw):
        st, hdr, body = self.call("POST", "/api/login", {"password": pw}, **kw)
        cookie = hdr.get("set-cookie", [""])[0]
        return st, cookie, {"Cookie": cookie.split(";")[0]} if cookie else {}

    def test_nothing_is_visible_before_login(self):
        st, hdr, body = self.call("GET", "/")
        self.assertEqual(st, 200); self.assertIn(b"Password", body)
        self.assertNotIn(b"renderSignals", body); self.assertNotIn(b"/api/state", body)
        for path in ("/app.js", "/index.html", "/anything"):
            st, _, body = self.call("GET", path)
            self.assertNotIn(b"renderSignals", body)
        for path in ("/api/state", "/api/anything"):
            self.assertEqual(self.call("GET", path)[0], 401)
        self.assertEqual(self.call("POST", "/api/wallets", {"chain": "solana", "address": "x"})[0], 401)
        self.assertEqual(self.call("POST", "/api/poll", {})[0], 401)
        self.assertEqual(self.call("DELETE", "/api/wallets/1")[0], 401)
        self.assertEqual(self.call("GET", "/api/state", headers={"Authorization": "Basic YWRtaW46eA=="})[0], 401)   # no Basic auth
        self.assertEqual(self.call("GET", "/api/state", headers={"Cookie": "ww_session=forged"})[0], 401)

    def test_public_files_have_the_right_type(self):
        st, hdr, body = self.call("GET", "/login.js")
        self.assertEqual((st, hdr["content-type"][0].split(";")[0]), (200, "application/javascript")); self.assertIn(b"/api/login", body)
        st, hdr, body = self.call("GET", "/login.html")
        self.assertEqual(hdr["content-type"][0].split(";")[0], "text/html"); self.assertIn(b'id="pw"', body)
        self.assertEqual(self.call("GET", "/login")[2], body)
        self.assertEqual(self.call("GET", "/manifest.json")[1]["content-type"][0], "application/manifest+json")
        self.assertEqual(self.call("GET", "/icon-192.png")[1]["content-type"][0], "image/png")

    def test_login_cookie_flags_and_full_session(self):
        st, cookie, ck = self.login()
        self.assertEqual(st, 200)
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=604800"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Secure", cookie)                                       # plain http (local testing)
        st, body = self.call("GET", "/api/state", headers=ck)[::2]
        self.assertEqual(st, 200); self.assertTrue(json.loads(body)["auth_enabled"])
        self.assertIn(b"renderSignals", self.call("GET", "/app.js", headers=ck)[2])     # panel code only after login
        self.assertEqual(self.call("POST", "/api/wallets", {"chain": "solana", "address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}, ck)[0], 201)
        st, hdr, _ = self.call("POST", "/api/logout", {}, ck)
        self.assertIn("Max-Age=0", hdr["set-cookie"][0])
        self.assertEqual(self.call("GET", "/api/state", headers=ck)[0], 401)     # that cookie is dead now

    def test_cookie_is_secure_over_https_and_hsts_is_sent(self):
        st, cookie, ck = self.login(https=True)
        self.assertIn("Secure", cookie)
        st, hdr, _ = self.call("GET", "/api/state", headers=ck, https=True)
        self.assertIn("max-age=31536000", hdr["strict-transport-security"][0])
        self.assertNotIn("strict-transport-security", self.call("GET", "/api/state", headers=ck)[1])

    def test_password_is_never_sent_to_the_browser(self):
        _, _, ck = self.login()
        seen = b""
        for path, h in (("/", {}), ("/login.js", {}), ("/login.html", {}), ("/api/auth", {}), ("/api/state", ck), ("/app.js", ck), ("/", ck), ("/manifest.json", {})):
            st, hdr, body = self.call("GET", path, headers=h)
            seen += body + json.dumps(hdr).encode()
        seen += self.call("POST", "/api/login", {"password": "wrong"})[2]
        for secret in (PW.encode(), os.environ["APP_PASSWORD_HASH"].encode(), os.environ["APP_PASSWORD_HASH"].split("$")[3].encode()):
            self.assertNotIn(secret, seen)

    def test_cross_site_requests_are_blocked(self):
        _, _, ck = self.login()
        evil = dict(ck, Origin="https://evil.example")
        self.assertEqual(self.call("POST", "/api/wallets", {"chain": "solana", "address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}, evil)[0], 403)
        self.assertEqual(self.call("DELETE", "/api/wallets/1", headers=evil)[0], 403)
        self.assertEqual(self.call("POST", "/api/login", {"password": PW}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("POST", "/api/logout", {}, evil)[0], 403)
        self.assertEqual(self.call("POST", "/api/wallets", {"chain": "solana", "address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}, dict(ck, Origin="http://panel.example"))[0], 201)

    def test_security_headers_everywhere(self):
        for path in ("/", "/api/state", "/login.js", "/nope"):
            st, hdr, _ = self.call("GET", path)
            self.assertEqual(hdr["x-content-type-options"], ["nosniff"]); self.assertEqual(hdr["x-frame-options"], ["DENY"])
            self.assertEqual(hdr["referrer-policy"], ["no-referrer"])
            csp = hdr["content-security-policy"][0]
            self.assertIn("default-src 'none'", csp); self.assertIn("frame-ancestors 'none'", csp)
            self.assertNotIn("unsafe-inline'", csp.split("script-src")[1].split(";")[0])    # no inline scripts allowed

    def test_brute_force_is_stopped_over_http(self):
        for _ in range(5):
            self.assertEqual(self.call("POST", "/api/login", {"password": "guess"}, ip="6.6.6.6")[0], 401)
        st, hdr, body = self.call("POST", "/api/login", {"password": PW}, ip="6.6.6.6")
        self.assertEqual(st, 429); self.assertIn("retry-after", hdr); self.assertIn(b"Too many", body)
        self.assertEqual(self.login(ip="4.4.4.4")[0], 200)

    def test_forged_forwarded_for_cannot_dodge_the_lockout(self):
        os.environ["TRUST_PROXY"] = "1"                                          # behind a proxy that appends the real IP last
        for i in range(5):
            self.call("POST", "/api/login", {"password": "guess"}, headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.7"})
        st = self.call("POST", "/api/login", {"password": PW}, headers={"X-Forwarded-For": "10.9.9.9, 203.0.113.7"})[0]
        self.assertEqual(st, 429)
        os.environ.pop("TRUST_PROXY")                                            # not behind a proxy: header is ignored entirely
        for _ in range(5):
            self.call("POST", "/api/login", {"password": "guess"}, ip="2.2.2.2", headers={"X-Forwarded-For": "1.2.3.4"})
        self.assertEqual(self.call("POST", "/api/login", {"password": PW}, ip="2.2.2.2", headers={"X-Forwarded-For": "5.6.7.8"})[0], 429)

    def test_authenticator_step_over_http(self):
        os.environ["TOTP_SECRET"] = RFC_SECRET
        self.assertTrue(json.loads(self.call("GET", "/api/auth")[2])["totp"])
        self.assertEqual(self.call("POST", "/api/login", {"password": PW})[0], 401)
        import time
        code = auth.totp_code(RFC_SECRET, int(time.time() // 30))
        st, hdr, _ = self.call("POST", "/api/login", {"password": PW, "code": code})
        self.assertEqual(st, 200)

    def test_bad_login_requests_do_not_crash(self):
        for body in ({}, {"password": 123}, {"password": "x" * 500}, {"password": PW, "code": 5}, []):
            st = self.call("POST", "/api/login", body, ip="3.3.3.3")[0]
            self.assertIn(st, (400, 401))

    def test_site_refuses_to_run_open_without_a_password(self):
        os.environ.pop("APP_PASSWORD_HASH")
        st, _, body = self.call("GET", "/")
        self.assertEqual(st, 503); self.assertIn(b"locked", body)
        self.assertEqual(self.call("GET", "/api/state")[0], 503)
        self.assertEqual(self.call("POST", "/api/wallets", {})[0], 503)
        self.assertEqual(self.call("POST", "/api/login", {"password": "anything at all"})[0], 400)
        os.environ["MOCK"] = "1"                                                 # demo mode may run open
        self.assertEqual(self.call("GET", "/api/state")[0], 200)


class SetPasswordTool(EnvCase):
    def setUp(self):
        super().setUp()
        import setpassword
        self.sp, self.folder, self.out = setpassword, tempfile.mkdtemp(), []
        os.environ["DB_PATH"] = os.path.join(self.folder, "x.db")

    def run_tool(self, argv, answers=()):
        it = iter(answers)
        return self.sp.run(argv, ask=lambda p: next(it), out=self.out.append, folder=self.folder)

    def env(self):
        with open(os.path.join(self.folder, ".env")) as f:
            return f.read()

    def test_saves_only_a_hash_and_replaces_old_plain_password(self):
        auth.write_env(os.path.join(self.folder, ".env"), {"APP_PASSWORD": "oldplain", "HELIUS_API_KEY": "keep"})
        self.assertEqual(self.run_tool([], [PW, PW]), 0)
        text = self.env()
        self.assertNotIn(PW, text); self.assertNotIn("oldplain", text)
        self.assertIn("HELIUS_API_KEY=keep", text)
        h = [l for l in text.splitlines() if l.startswith("APP_PASSWORD_HASH=")][0].split("=", 1)[1]
        self.assertTrue(auth.check_hash(PW, h))
        if os.name == "posix":
            self.assertEqual(os.stat(os.path.join(self.folder, ".env")).st_mode & 0o077, 0)   # only the owner can read it

    def test_rejects_weak_and_mismatched_passwords(self):
        self.assertEqual(self.run_tool([], ["short", "short"]), 1)
        self.assertEqual(self.run_tool([], [PW, PW + "1"]), 1)
        self.assertFalse(os.path.exists(os.path.join(self.folder, ".env")))

    def test_generate_totp_and_unlock(self):
        self.assertEqual(self.run_tool(["--generate"]), 0)
        pw = [l.strip() for l in self.out if l.startswith("    ")][0]
        self.assertGreaterEqual(len(pw), 20)
        h = [l for l in self.env().splitlines() if l.startswith("APP_PASSWORD_HASH=")][0].split("=", 1)[1]
        self.assertTrue(auth.check_hash(pw, h))
        self.assertEqual(self.run_tool(["--totp"]), 0)
        secret = [l for l in self.env().splitlines() if l.startswith("TOTP_SECRET=")][0].split("=", 1)[1]
        self.assertEqual(len(secret), 32); self.assertTrue(any(secret in l for l in self.out))
        self.assertEqual(self.run_tool(["--no-totp"]), 0); self.assertNotIn("TOTP_SECRET", self.env())
        d = dbmod.DB(os.environ["DB_PATH"]); d.add_attempt("1.1.1.1", 1e12); d.add_session("t", "f", 1, 2e12, "ip")
        self.assertEqual(self.run_tool(["--unlock", "--logout-all"]), 0)
        self.assertEqual((d.attempts_since("1.1.1.1", 0), d.get_session("t")), ([], None))


if __name__ == "__main__":
    unittest.main()
