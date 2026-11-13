# WalletWatch

A website (installs on your phone like an app) that watches several wallets and alerts you when
**two or more of them buy the same token**. It then ranks those tokens with a transparent 0-100 signal score.

Supports Solana, Ethereum, Robinhood Chain and Arc. No dependencies: Python 3.9+ only.

## 1. Try it in 30 seconds (fake data)

    MOCK=1 python server.py        # Windows PowerShell:  $env:MOCK=1; python server.py

Open http://localhost:8080. To open it on your phone (same Wi-Fi) use http://YOUR-PC-IP:8080.

## 2. Use it for real

    python server.py

Open the site, go to **Wallets**, paste public addresses. Solana buys are pushed to the app within 1-3 seconds of confirming (websocket trigger); other chains within `POLL_SECONDS` (default 5).

**For real speed use a private Solana RPC (Helius/QuickNode free tier).** The public Solana RPC rate-limits and will make alerts late.

### Chains and RPC settings (environment variables)

| Variable | Notes |
|---|---|
| `SOLANA_RPC_URL` | Defaults to the public Solana RPC, which rate-limits. For 5+ wallets use a free Helius or QuickNode URL. |
| `ETHEREUM_RPC_URL` | Defaults to a public endpoint. |
| `ROBINHOOD_RPC_URL` | **Required** for Robinhood Chain. Get the URL from Robinhood Chain's docs. |
| `ARC_RPC_URL` | **Required** for Arc. Get the URL from Arc's docs. |
| `POLL_SECONDS` | Check interval per wallet, default 5 (minimum 2). |
| `SOLANA_WS_URL` | Websocket for instant Solana triggers. Derived from `SOLANA_RPC_URL` if not set. `USE_WS=0` turns it off. |
| `SAFETY_POLL_SECONDS` | Backup poll for Solana wallets while the websocket is live, default 30. |
| `WORKERS` | Wallets checked in parallel, default 16. |
| `MAX_WALLETS` | Wallet limit, default 500. |
| `NOTIFY_FIRST_BUYS` | `0` stops the alert for each wallet's first-ever buy of a coin (multi-wallet alerts stay on). |
| `APP_PASSWORD_HASH` | Login password as a salted hash. Create it with `python setpassword.py` (never type the password into `.env`). |
| `TOTP_SECRET` | Optional authenticator-app code at login. Create with `python setpassword.py --totp`. |
| `TRUST_PROXY` | Set `1` only when the site is behind a proxy you control (PythonAnywhere, nginx, Caddy), so failed-login lockouts use the visitor's real address. |
| `SESSION_DAYS` / `SESSION_IDLE_HOURS` | How long a login lasts (default 7 days, or 12 hours without use). |
| `ALLOW_NO_PASSWORD` | `1` lets the site run without a password (local testing only). Without a password the site refuses to open. |
| `NTFY_TOPIC` | Phone alerts when the site is closed, via the free ntfy app. Pick a long random topic name. |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Same, via a Telegram bot. |
| `DB_PATH` | SQLite file location, default `walletwatch.db`. |
| `IGNORE_TOKENS` | Comma-separated extra token addresses to never treat as a buy. |

## 3. Put it online so it runs 24/7 and alerts your phone

It has to run somewhere that stays on. Any host that runs Python or Docker works (Render, Railway, Fly.io, a $5 VPS).
Use the included `Dockerfile` (or `Procfile`), set the variables above, and **attach a persistent disk at `/data`**
or your wallet list resets on every redeploy. Free tiers that "sleep" will miss buys, so avoid them.

Then open the HTTPS URL in Chrome on Android and choose **Add to Home screen**. It launches full-screen like an app.

## Login and security

Open the website and you get a login page first. Nothing else (no panel, no data, no panel code) is sent until the password is accepted.

    python setpassword.py              choose a password (typed hidden)
    python setpassword.py --generate   make a strong random one for you
    python setpassword.py --totp       also ask for a 6-digit authenticator code
    python setpassword.py --logout-all log out every browser
    python setpassword.py --unlock     clear a lockout

* The password is checked **only on the server**. It is stored as a PBKDF2-SHA256 hash (600,000 rounds, random salt) in `.env`, so even someone who reads that file cannot see it. It is never in the page, the scripts, the browser storage or any response.
* After login the browser gets a random 256-bit cookie that JavaScript cannot read (HttpOnly), is not sent from other websites (SameSite=Strict) and is Secure on https. The server keeps only a hash of it. Changing the password ends every login.
* 5 wrong attempts lock that address for 15 minutes; every wrong attempt is also slowed down.
* Requests from other websites are refused, and strict browser security headers are sent (no inline scripts, no framing, no sniffing).
* **Use https.** On plain http a password can be read on the network. PythonAnywhere, Caddy or Cloudflare give you https; see `DEPLOY_PYTHONANYWHERE.md`.
* No website can honestly be called unhackable. This setup closes the usual attacks. The two things that matter most now are a long unique password (or `--generate`) and the authenticator code.

## Want it at a public web address?

See `DEPLOY_PYTHONANYWHERE.md`. On a hosting service the project runs as a website (`wsgi_app.py`) plus an always-on worker (`worker.py`); on your own PC just `python server.py`.

## Quick start with Helius (do this first)

    python check.py YOUR_FULL_HELIUS_KEY     saves the key in .env and tests everything
    python server.py                         starts the app

`check.py` tells you in plain words what works and what does not (key, internet, wallet reading, instant alerts). The Wallets tab also shows a **Connection** card with the same information while the app runs. The Helius SDK (JavaScript) is not needed: this project talks to the same Helius address directly.

## Helius setup (details)

1. Copy `.env.example` to `.env` (same folder as `server.py`).
2. Put your key in it: `HELIUS_API_KEY=your-full-key`.
3. Run `python server.py`. The RPC and websocket addresses are built automatically and the app keeps to Helius's 10 requests/second limit (it uses 9). Change with `HELIUS_RPS` if your plan allows more.

## Staying inside 1,000,000 Helius credits a month

Helius counts 1 credit per request (getTransaction, getSignaturesForAddress ...) and 2 credits per 0.1 MB of websocket data. WalletWatch is built around that:

* **Push, not polling.** The websocket tells the app which transaction just happened. The app downloads only that one transaction: **1 credit per alert** (+1 the first time a wallet buys a coin, to prove it is the first ever).
* **Free filtering.** Transactions whose logs show no token program (plain SOL transfers etc.) are skipped without any request.
* **No double downloads.** A transaction is never fetched twice, even if the safety check sees it again.
* **Safety check.** Each wallet is also listed every 15 minutes (`RECONCILE_SECONDS`) so nothing missed during a disconnect is lost. If the websocket is down, polling slows itself to fit the budget.
* **Budget guard.** Usage is counted and saved in `walletwatch.db` (survives restarts) and shown on the Wallets tab. If you spend faster than planned the app switches to Saving mode (fewer background checks); at 92% of the month (`BUDGET_RESERVE`) it pauses and resumes next cycle, so Helius never has to refuse you.
* **Rate limit.** Requests are spaced to 9 per second (`HELIUS_RPS`).

Rough numbers for 23 wallets: safety checks about 2,200 credits/day (about 66,000/month); the remaining ~850,000 credits cover roughly 28,000 buy-like transactions per day across all wallets. If your billing cycle does not start on the 1st, set `HELIUS_CYCLE_DAY` (for example `15`). The number in the Helius dashboard is the final truth; the app keeps an 8% safety margin for the difference.

## The tabs

* **Signals** - one row per coin that 2+ of your wallets bought. Tap **Details** for the popup (which wallets, score, price, liquidity, chart link).
* **Graph** - only coins bought by 2+ of your wallets: how many wallets bought each, and how that grew over time. Live.
* **Activity** - the first time EVER a wallet buys a coin. Repeat buys never appear here.
* **All Buys** - every buy transaction of every wallet, newest first, including repeat buys. A green NEW COIN tag marks a first-ever buy.
* **Wallets** - add/remove wallets.

## If you see "HTTP 429"

That is the free public Solana server refusing too many requests. The app now spaces its requests and retries by itself, but the public server is still slow and unreliable. Fix it for good in 2 minutes: create a free key at helius.dev (or quicknode.com), then before starting:

    PowerShell:  $env:SOLANA_RPC_URL = "https://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
    then:        python server.py

`RPC_MAX_RPS` overrides the request speed limit (default 2.5/s for the public Solana server, 20/s for any other).

## What changed in this version

* **First-buy only.** Each wallet is recorded once per coin. Buying the same coin again never changes the dashboard.
* **First EVER, verified.** Solana: the wallet's token account must have no older history. EVM: no earlier transfer of that token to the wallet (if the RPC can't answer, it counts as first seen).
* **Faster.** Wallets are checked in parallel; Solana pushes via websocket; reads use `confirmed` (not the ~13 s slower `finalized`); market lookups no longer block alerts.
* **More accurate.** A transaction the RPC hasn't indexed yet is retried instead of skipped; more than one page of new transactions is read.
* **Graph tab.** Live bars of wallets per coin plus growth lines over time.

## How it works

1. Each wallet is read straight from the chain (Solana: `getSignaturesForAddress` + `getTransaction`;
   EVM: `eth_getLogs` for ERC-20 transfers).
2. A **buy** = the wallet's balance of a token went up and it paid for it in the same transaction (SOL/ETH or another token).
   Plain transfers, airdrops, sells, wrapped native tokens and big stablecoins are ignored.
3. Market data (price, liquidity, 24h change, pair age) comes from DexScreener's free API.
4. Buys within your time window (default 6h) are grouped by token. Tokens bought by enough distinct wallets become signals.
5. Score = 45 wallets in agreement + 15 recency + 25 liquidity + 15 not-already-pumped. Risk flags explain the weak spots.

## Honest limits

* Buy detection is a heuristic. Smart-contract wallets, unusual routers or bridges can be missed or misread.
* If a wallet makes more than 25 transactions between checks (Solana), the oldest are skipped.
* Robinhood Chain and Arc tokens usually aren't on DexScreener, so they show "no market data" and rate lower.
* Birdeye is not scraped. Its site is bot-protected and breaks scrapers; on-chain reads are more accurate and real-time.
* The signal score ranks agreement and risk. It cannot predict prices and is not financial advice.

## Tests

    python -m unittest discover -s tests
