import time, json, os, threading
from flask import Flask, request, jsonify
from datetime import datetime
from pybit.unified_trading import HTTP

app = Flask(__name__)
client = HTTP(testnet=False, api_key="0ELcF3VdNwea9OjqgL", api_secret="GW1aOjw1WgSQzzfNtjnox4XpciKn888MTI2y")
SYMBOL = "FARTCOINUSDT"
LEVERAGE = 15
MARGIN_PER_TRADE = 30
STATE_FILE = "bot_state.json"
TP1_PCT = 0.50
TP2_PCT = 0.25
state_lock = threading.Lock()

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            d = json.load(f)
            d.setdefault("last_tp_profit", 0.0)
            d.setdefault("tp1_done", False)
            d.setdefault("tp2_done", False)
            return d
    return {"position": None, "entry_price": None, "last_tp_profit": 0.0, "tp1_done": False, "tp2_done": False}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f)

state = load_state()

def log(m): print("["+datetime.now().strftime("%H:%M:%S")+"] "+m, flush=True)

def get_price():
    try: return float(client.get_tickers(category="linear", symbol=SYMBOL)["result"]["list"][0]["lastPrice"])
    except Exception as e: log("PRICE ERR:"+str(e)); return None

def in_profit():
    p = get_price()
    if p is None or state["entry_price"] is None: return False
    e = float(str(state["entry_price"]))
    if state["position"] == "long": return p > e
    if state["position"] == "short": return p < e
    return False

def get_size():
    try:
        p = float(client.get_tickers(category="linear", symbol=SYMBOL)["result"]["list"][0]["lastPrice"])
        s = int(round(MARGIN_PER_TRADE * LEVERAGE / p))
        log("Size:"+str(s)+" @ $"+str(p)); return max(s, 1)
    except Exception as e: log("SIZE ERR:"+str(e)); return 1

def set_lev():
    try: client.set_leverage(category="linear", symbol=SYMBOL, buyLeverage=str(LEVERAGE), sellLeverage=str(LEVERAGE)); log("LEV OK")
    except Exception as e: log("LEV ERR:"+str(e))

def close_all():
    try:
        for p in client.get_positions(category="linear", symbol=SYMBOL)["result"]["list"]:
            if float(p["size"]) > 0:
                client.place_order(category="linear", symbol=SYMBOL, side="Sell" if p["side"] == "Buy" else "Buy", orderType="Market", qty=p["size"], reduceOnly=True)
        log("CLOSE OK"); return True
    except Exception as e: log("CLOSE ERR:"+str(e)); return False

def order(side, size):
    try: r = client.place_order(category="linear", symbol=SYMBOL, side=side, orderType="Market", qty=str(size)); log("ORDER "+side+" "+str(size)); return r
    except Exception as e: log("ORDER ERR:"+str(e)); return {}

def pos_size():
    try:
        for p in client.get_positions(category="linear", symbol=SYMBOL)["result"]["list"]:
            if float(p["size"]) > 0: return float(p["size"]), p["side"]
        return 0, None
    except: return 0, None

def add_margin():
    with state_lock:
        profit = state.get("last_tp_profit", 0)
        if profit <= 0: log("Sin profits"); return
        p = get_price()
        if p is None: return
        s = int(round(profit * LEVERAGE / p))
        if s < 1: return
        side = "Buy" if state["position"] == "long" else "Sell"
        try:
            client.place_order(category="linear", symbol=SYMBOL, side=side, orderType="Market", qty=str(s))
            log("MARGIN ADD $"+str(profit)+"->"+str(s))
            state["last_tp_profit"] = 0.0
            save_state(state)
        except Exception as e: log("MARGIN ERR:"+str(e))

def exec_tp(label, pct, flag):
    with state_lock:
        if state.get(flag, False): log(label+" ya ejecutado"); return
        if not in_profit(): log(label+" en perdida"); return
        cs, side = pos_size()
        if cs == 0: return
        ts = int(round(cs * pct))
        if ts < 1: return
        cs2 = "Sell" if side == "Buy" else "Buy"
        try:
            p = get_price()
            client.place_order(category="linear", symbol=SYMBOL, side=cs2, orderType="Market", qty=str(ts), reduceOnly=True)
            e = float(str(state["entry_price"]))
            profit = round(ts * (p - e), 2) if state["position"] == "long" else round(ts * (e - p), 2)
            if profit > 0: state["last_tp_profit"] = state.get("last_tp_profit", 0) + profit
            state[flag] = True
            save_state(state)
            log(label+" OK "+str(ts)+" profit $"+str(profit))
        except Exception as e: log(label+" ERR:"+str(e))

def reset():
    state["last_tp_profit"] = 0.0
    state["tp1_done"] = False
    state["tp2_done"] = False

def process(data):
    action = data.get("action")
    log("Procesando:"+str(action))

    if action == "long":
        with state_lock:
            al = state["position"] == "long"
            hp = state.get("last_tp_profit", 0) > 0
        if al:
            if hp: add_margin()
            else: log("LONG rep sin profit")
            return
        log("LONG @ "+str(data.get("price"))); close_all(); time.sleep(2); set_lev(); time.sleep(1)
        s = get_size(); order("Buy", s)
        with state_lock:
            state["position"] = "long"; state["entry_price"] = float(str(data.get("price"))); reset(); save_state(state)

    elif action == "short":
        with state_lock:
            ash = state["position"] == "short"
            hp = state.get("last_tp_profit", 0) > 0
        if ash:
            if hp: add_margin()
            else: log("SHORT rep sin profit")
            return
        log("SHORT @ "+str(data.get("price"))); close_all(); time.sleep(2); set_lev(); time.sleep(1)
        s = get_size(); order("Sell", s)
        with state_lock:
            state["position"] = "short"; state["entry_price"] = float(str(data.get("price"))); reset(); save_state(state)

    elif action == "long_repeat":
        with state_lock:
            pos = state["position"]
            hp = state.get("last_tp_profit", 0) > 0
        if pos == "long":
            if hp: add_margin()
            else: log("LONG repeat sin profit - ignorando")
        else:
            log("LONG repeat ignorado - no hay posicion long")

    elif action == "short_repeat":
        with state_lock:
            pos = state["position"]
            hp = state.get("last_tp_profit", 0) > 0
        if pos == "short":
            if hp: add_margin()
            else: log("SHORT repeat sin profit - ignorando")
        else:
            log("SHORT repeat ignorado - no hay posicion short")

    elif action == "tp1":
        if state["position"] is None: return
        exec_tp("TP1 15m 58/40 (50%)", TP1_PCT, "tp1_done")

    elif action == "tp2":
        if state["position"] is None: return
        exec_tp("TP2 15m 65/33 (25%)", TP2_PCT, "tp2_done")

    else:
        log("Desconocido:"+str(action))

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(); log("Signal:"+str(data))
    threading.Thread(target=process, args=(data,), daemon=True).start()
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    print("MWhalekiller FART 15m v2 | TP1 50% RSI15m 58/40 | TP2 25% RSI15m 65/33 | Resto 25% libre")
    print("Estado:"+str(state))
    app.run(host="0.0.0.0", port=5000)
