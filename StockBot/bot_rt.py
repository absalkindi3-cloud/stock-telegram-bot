import os, json, time, asyncio, traceback, re
from typing import Dict, Any, Set, Optional, List, Tuple

import requests
import websockets
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

# ====== ENV ======
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ALPACA_KEY = os.environ.get("ALPACA_KEY", "").strip()
ALPACA_SECRET = os.environ.get("ALPACA_SECRET", "").strip()

# Alpaca free feed: IEX
ALPACA_WS = "wss://stream.data.alpaca.markets/v2/iex"
ALPACA_REST_BASE = "https://data.alpaca.markets"

DEFAULT_DROP_PCT = 0.02   # 2%
ASK_OUTLIER_PCT = 0.02    # إذا ASK بعيد عن LAST بأكثر من 2% نعتبره شاذ
STATE_FILE = "watch_state.json"

# state[user_id][ticker] = {
#   "start_price": float|None,
#   "started_at": int,
#   "start_notified": bool,
#   "drop_pct": float,
#   "start_source": str|None
# }
state: Dict[str, Dict[str, Any]] = {}
state_lock = asyncio.Lock()


def load_state():
    global state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}


def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def norm_ticker(t: str) -> str:
    return t.strip().upper().replace(" ", "")


def alpaca_headers():
    return {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}


def tickers_unlocked() -> Set[str]:
    s: Set[str] = set()
    for um in state.values():
        s.update(um.keys())
    return s


def parse_input(text: str) -> Tuple[str, float]:
    """
    يدعم:
    - AAPL
    - AAPL 3
    - AAPL 3%
    - TSLA 1.5
    - HIMS4  -> ticker=HIMS, pct=4
    - AAPL2.5 -> ticker=AAPL, pct=2.5
    """
    s = text.strip()

    # 1) split by spaces
    parts = s.split()
    if len(parts) >= 2:
        ticker = norm_ticker(parts[0])
        raw = parts[1].strip().replace("%", "")
        try:
            val = float(raw)
            if 0 < val < 100:
                return ticker, val / 100.0
        except Exception:
            pass
        return ticker, DEFAULT_DROP_PCT

    # 2) no spaces: suffix number
    m = re.match(r"^([A-Za-z]+)(\d+(?:\.\d+)?)$", s)
    if m:
        ticker = norm_ticker(m.group(1))
        try:
            val = float(m.group(2))
            if 0 < val < 100:
                return ticker, val / 100.0
        except Exception:
            pass
        return ticker, DEFAULT_DROP_PCT

    # 3) just ticker
    return norm_ticker(s), DEFAULT_DROP_PCT


def get_snapshot_price_best(ticker: str) -> Tuple[Optional[float], Optional[str]]:
    """
    أقرب لـ eToro لكن مع حماية من ASK الشاذ:
    - إذا LAST موجود: استخدم ASK فقط لو قريب من LAST (<= 2% فرق)، وإلا استخدم LAST
    - إذا لا يوجد LAST: جرّب ASK ثم MID ثم BID
    Returns (price, source)
    """
    url = f"{ALPACA_REST_BASE}/v2/stocks/{ticker}/snapshot"
    try:
        r = requests.get(url, headers=alpaca_headers(), timeout=10)
        if r.status_code != 200:
            print(f"❌ Snapshot {ticker} status={r.status_code} body={r.text[:200]}")
            return None, None

        data = r.json()
        lq = data.get("latestQuote") or {}
        lt = data.get("latestTrade") or {}

        ap = lq.get("ap")
        bp = lq.get("bp")
        lp = lt.get("p")

        ask = float(ap) if ap is not None else None
        bid = float(bp) if bp is not None else None
        last = float(lp) if lp is not None else None

        # Prefer LAST base if exists
        if last is not None and last > 0:
            # Use ASK only if it's reasonable vs LAST
            if ask is not None and ask > 0:
                if abs(ask - last) / last <= ASK_OUTLIER_PCT:
                    return ask, "ASK"
                else:
                    return last, "LAST (ASK outlier)"
            return last, "LAST"

        # No LAST -> try ASK then MID then BID
        if ask is not None and ask > 0:
            return ask, "ASK"
        if ask is not None and bid is not None and ask > 0 and bid > 0:
            return (ask + bid) / 2.0, "MID"
        if bid is not None and bid > 0:
            return bid, "BID"

        return None, None
    except Exception as e:
        print("❌ Snapshot exception:", repr(e))
        return None, None


# ===== Telegram handlers =====
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    txt = (update.message.text or "").strip()
    if not txt or txt.startswith("/"):
        return

    ticker, drop_pct = parse_input(txt)
    user_id = str(update.effective_user.id)

    async with state_lock:
        state.setdefault(user_id, {})[ticker] = {
            "start_price": None,
            "started_at": int(time.time()),
            "start_notified": False,
            "drop_pct": drop_pct,
            "start_source": None,
        }
        save_state()

    price, source = get_snapshot_price_best(ticker)

    if price is not None and price > 0:
        async with state_lock:
            if ticker in state.get(user_id, {}):
                state[user_id][ticker]["start_price"] = price
                state[user_id][ticker]["start_notified"] = True
                state[user_id][ticker]["start_source"] = source
                save_state()

        thr = price * (1 - drop_pct)
        await update.message.reply_text(
            f"📍 بدأنا المراقبة\n"
            f"{ticker}\n"
            f"مصدر السعر: {source}\n"
            f"سعر البداية: {price}\n"
            f"نسبة التنبيه: {drop_pct*100:.2f}%\n"
            f"سعر التنبيه: {thr:.4f}"
        )
    else:
        await update.message.reply_text(
            f"✅ سجلت {ticker}\n"
            f"نسبة التنبيه: {drop_pct*100:.2f}%\n"
            f"⏳ ما قدرنا نجيب سعر صالح الآن. سأثبت السعر من الستريم عند أول Quote/Trade صالح."
        )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    user_id = str(update.effective_user.id)
    async with state_lock:
        um = dict(state.get(user_id, {}))

    if not um:
        return await update.message.reply_text("ما عندك أسهم. مثال: AAPL أو AAPL 3 أو HIMS4")

    lines = []
    for t, info in um.items():
        dp = float(info.get("drop_pct", DEFAULT_DROP_PCT))
        sp = info.get("start_price")
        src = info.get("start_source") or "-"
        if sp is None:
            lines.append(f"- {t} | drop={dp*100:.2f}% | waiting price")
        else:
            sp = float(sp)
            lines.append(f"- {t} | drop={dp*100:.2f}% | start={sp} ({src}) | alert≤{sp*(1-dp):.4f}")

    await update.message.reply_text("📌 الأسهم المراقبة:\n" + "\n".join(lines))


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not context.args:
        return await update.message.reply_text("اكتب: /stop AAPL")

    ticker = norm_ticker(context.args[0])
    user_id = str(update.effective_user.id)

    async with state_lock:
        user_map = state.get(user_id, {})
        if ticker in user_map:
            user_map.pop(ticker, None)
            save_state()
            return await update.message.reply_text(f"🛑 تم إيقاف {ticker}")

    await update.message.reply_text(f"{ticker} غير موجود ضمن المراقبة.")


async def cmd_stopall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    user_id = str(update.effective_user.id)
    async with state_lock:
        state[user_id] = {}
        save_state()
    await update.message.reply_text("🛑 تم إيقاف كل الأسهم.")


# ===== Stream handlers =====
async def alpaca_stream_loop(app: Application):
    last_sub: Set[str] = set()

    while True:
        try:
            async with websockets.connect(ALPACA_WS, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"action": "auth", "key": ALPACA_KEY, "secret": ALPACA_SECRET}))
                auth_msg = await ws.recv()
                print("✅ Stream auth response:", auth_msg)

                async with state_lock:
                    current = set(tickers_unlocked())

                await ws.send(json.dumps({"action": "subscribe", "quotes": sorted(current), "trades": sorted(current)}))
                last_sub = set(current)
                print("✅ Subscribed:", sorted(last_sub))

                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        msg = None

                    if msg:
                        data = json.loads(msg)
                        if isinstance(data, list):
                            for item in data:
                                T = item.get("T")
                                sym = item.get("S")
                                if not sym:
                                    continue

                                # Quotes: use ASK only if >0 and not outlier vs LAST (start_price if exists)
                                if T == "q":
                                    ap = item.get("ap")
                                    if ap is not None:
                                        apf = float(ap)
                                        if apf > 0:
                                            ok = True
                                            ref = None
                                            async with state_lock:
                                                for um in state.values():
                                                    if sym in um and um[sym].get("start_price") is not None:
                                                        ref = float(um[sym]["start_price"])
                                                        break
                                            if ref is not None and ref > 0:
                                                if abs(apf - ref) / ref > ASK_OUTLIER_PCT:
                                                    ok = False
                                            if ok:
                                                await handle_price(app, sym, apf, price_source="ASK")

                                # Trades fallback if start_price still None
                                if T == "t" and item.get("p") is not None:
                                    await handle_trade_fallback(app, sym, float(item["p"]))

                    # refresh subscriptions
                    async with state_lock:
                        current2 = set(tickers_unlocked())

                    if current2 != last_sub:
                        if last_sub:
                            await ws.send(json.dumps({"action": "unsubscribe", "quotes": sorted(last_sub), "trades": sorted(last_sub)}))
                        if current2:
                            await ws.send(json.dumps({"action": "subscribe", "quotes": sorted(current2), "trades": sorted(current2)}))
                        last_sub = set(current2)
                        print("🔄 Updated subscriptions:", sorted(last_sub))

        except Exception as e:
            print("❌ Stream error:", repr(e))
            traceback.print_exc()
            await asyncio.sleep(3)


async def handle_trade_fallback(app: Application, ticker: str, trade_price: float):
    if trade_price <= 0:
        return

    async with state_lock:
        changed = False
        for user_id, um in list(state.items()):
            info = um.get(ticker)
            if not info:
                continue

            if info.get("start_price") is None and not info.get("start_notified", False):
                info["start_price"] = trade_price
                info["start_notified"] = True
                info["start_source"] = "LAST"
                changed = True

                dp = float(info.get("drop_pct", DEFAULT_DROP_PCT))
                thr = trade_price * (1 - dp)
                asyncio.create_task(app.bot.send_message(
                    chat_id=int(user_id),
                    text=(
                        f"📍 بدأنا المراقبة\n"
                        f"{ticker}\n"
                        f"مصدر السعر: LAST (Fallback)\n"
                        f"سعر البداية: {trade_price}\n"
                        f"نسبة التنبيه: {dp*100:.2f}%\n"
                        f"سعر التنبيه: {thr:.4f}"
                    )
                ))
        if changed:
            save_state()


async def handle_price(app: Application, ticker: str, price: float, price_source: str = "ASK"):
    if price <= 0:
        return

    to_alert: List[Tuple[str, float, float, float]] = []

    async with state_lock:
        changed = False
        for user_id, um in list(state.items()):
            info = um.get(ticker)
            if not info:
                continue

            dp = float(info.get("drop_pct", DEFAULT_DROP_PCT))

            # If not set, set start_price from stream
            if info.get("start_price") is None:
                info["start_price"] = price
                info["start_source"] = price_source
                changed = True

            # Send start message only once
            if info.get("start_price") is not None and not info.get("start_notified", False):
                info["start_notified"] = True
                if not info.get("start_source"):
                    info["start_source"] = price_source
                changed = True

                sp0 = float(info["start_price"])
                thr = sp0 * (1 - dp)
                asyncio.create_task(app.bot.send_message(
                    chat_id=int(user_id),
                    text=(
                        f"📍 بدأنا المراقبة\n"
                        f"{ticker}\n"
                        f"مصدر السعر: {info.get('start_source')}\n"
                        f"سعر البداية: {sp0}\n"
                        f"نسبة التنبيه: {dp*100:.2f}%\n"
                        f"سعر التنبيه: {thr:.4f}"
                    )
                ))

            # Check drop
            sp_now = info.get("start_price")
            if sp_now is not None:
                spf = float(sp_now)
                if price <= spf * (1 - dp):
                    um.pop(ticker, None)
                    changed = True
                    to_alert.append((user_id, spf, price, dp))

        if changed:
            save_state()

    for user_id, spf, nowp, dp in to_alert:
        try:
            await app.bot.send_message(
                chat_id=int(user_id),
                text=(
                    f"🚨 تنبيه هبوط {dp*100:.2f}%\n"
                    f"{ticker}\n"
                    f"سعر البداية: {spf}\n"
                    f"السعر الآن: {nowp}\n"
                    f"✅ تم إيقاف المراقبة بعد التنبيه."
                )
            )
        except Exception:
            pass


def ensure_windows_event_loop():
    if os.name == "nt":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing")
    if not ALPACA_KEY or not ALPACA_SECRET:
        raise RuntimeError("ALPACA_KEY / ALPACA_SECRET missing")

    ensure_windows_event_loop()
    load_state()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("stopall", cmd_stopall))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def on_start(app_: Application):
        asyncio.create_task(alpaca_stream_loop(app_))
        print("✅ Bot is running...")

    app.post_init = on_start
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
