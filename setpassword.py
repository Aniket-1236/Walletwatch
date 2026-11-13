"""Set up (or change) the login of your WalletWatch website.

    python setpassword.py              choose a password (typed hidden, saved as a salted hash in .env)
    python setpassword.py --generate   create a strong random password for you and show it once
    python setpassword.py --totp       also require a 6-digit code from an authenticator app
    python setpassword.py --no-totp    remove the authenticator requirement
    python setpassword.py --logout-all log out every browser right now
    python setpassword.py --unlock     clear the lock after too many wrong attempts

After changing anything, restart the app (and the worker, if you use one).
Changing the password or the authenticator also logs out every browser that was logged in."""
import getpass
import os
import secrets
import sys

import auth
import config

HERE = os.path.dirname(os.path.abspath(__file__))
WORDS = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate():
    return "-".join("".join(secrets.choice(WORDS) for _ in range(5)) for _ in range(4))   # 20 random characters


def run(argv=None, ask=getpass.getpass, out=print, folder=HERE):
    argv = argv if argv is not None else sys.argv[1:]
    env = os.path.join(folder, ".env")
    if "--unlock" in argv or "--logout-all" in argv:
        import db as dbmod
        d = dbmod.DB(config.db_path())
        if "--unlock" in argv:
            d.clear_attempts()
            out("Done: failed-attempt lock cleared.")
        if "--logout-all" in argv:
            d.clear_sessions()
            out("Done: every browser is logged out.")
        return 0
    if "--no-totp" in argv:
        auth.write_env(env, {}, remove=("TOTP_SECRET",))
        out("Authenticator requirement removed. Restart the app.")
        return 0
    if "--totp" in argv:
        secret = auth.new_totp_secret()
        auth.write_env(env, {"TOTP_SECRET": secret})
        out("Authenticator turned on. In Google Authenticator / Authy / Microsoft Authenticator choose")
        out("'Enter a setup key' and type:")
        out("    Account:  WalletWatch")
        out("    Key:      " + secret)
        out("    Type:     time based")
        out("(or open this link on the phone: " + auth.otpauth_uri(secret) + ")")
        out("Keep the key somewhere safe: it is the only way to set the app up again. Restart the app.")
        return 0

    if "--generate" in argv:
        pw = generate()
        out("Your new password (shown only now, save it in a password manager):")
        out("    " + pw)
    else:
        pw = ask("New password (typing is hidden): ")
        problem = auth.password_problem(pw)
        if problem:
            out(problem)
            return 1
        if ask("Type it again: ") != pw:
            out("The two passwords are not the same. Nothing was changed.")
            return 1
    auth.write_env(env, {"APP_PASSWORD_HASH": auth.hash_password(pw)}, remove=("APP_PASSWORD",))
    out(f"Saved. Only a salted hash of the password is stored in {env}, not the password itself.")
    out("Restart the app (and the worker). Everyone who was logged in is logged out.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
