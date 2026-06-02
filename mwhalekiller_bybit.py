import time
import json
import os
import threading
from flask import Flask, request, jsonify
from datetime import datetime
from pybit.unified_trading import HTTP

app = Flask(__name__)

client = HTTP(
    testnet=False,
    api_key="iOPGsG1k0Z6KAvkK0L",
    api_secret="Vl1VFj57Tye12TfihmXDxHHRrhGHBErzDXgm",
)

SYMBOL = "FARTCOINUSDT"
LEVERAGE = 15
MARGIN_PER_TRADE = 30
STATE_FILE = "bot_state.json"

TP_DAILY_PCT = 0.75  # 75% al cruce de RSI Daily

state_lock = threading.Lock()

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "position": None,
        "entry_price": None,
        "last_tp_profit": 0.0,
        "tp_daily_done": False,
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

state = load_state()

def log(msg):
    print("[" + datetime.now().strftime("%H:%M:%S") + "] " + msg, flush=True)

def get_current_price():
    try:
        ticker = client.get_tickers(category="linear", symbol=SYMBOL)
        return float(ticker["result"]["list"][0]["lastPrice"])
    except Exception as e:
        log("ERROR obteniendo precio: " + str(e))
        return None

def is_in_profit():
    price = get_current_price()
    if price is None or state["entry_price"] is None:
        return False
    entry = float(str(state["entry_price"]))
    if state["position"] == "long":
        return price > entry
    if state["position"] == "short":
        return price < entry
    return False

def get_order_size():
    try:
        ticker = client.get_tickers(category="linear", symbol=SYMBOL)
        price = float(ticker["result"]["list"][0]["lastPrice"])
        size = int(round(MARGIN_PER_TRADE * LEVERAGE / price))
        if size < 1:
            size = 1
        log("Size calculado: " + str(size) + " @ $" + str(price))
        return size
    except Exception as e:
        log("ERROR calculando size: " + str(e))
        return 1

def set_leverage():
    try:
        client.set_leverage(category="linear", symbol=SYMBOL, buyLeverage=str(LEVERAGE), sellLeverage=str(LEVERAGE))
        log("LEVERAGE OK")
    except Exception as e:
        log("LEVERAGE ERROR: " + str(e))

def close_all():
    try:
        positions = client.get_positions(category="linear", symbol=SYMBOL)
        for p in positions["result"]["list"]:
            if float(p["size"]) > 0:
                side = "Sell" if p["side"] == "Buy" else "Buy"
                client.place_order(category="linear", symbol=SYMBOL, side=side, orderType="Market", qty=p["size"], reduceOnly=True)
        log("CLOSE ALL OK")
        return True
    except Exception as e:
        log("CLOSE ERROR: " + str(e))
        return False

def place_order(side, size):
    try:
        result = client.place_order(category="linear", symbol=SYMBOL, side=side, orderType="Market", qty=str(size))
        log("ORDER " + side + " " + str(size) + ": " + str(result))
        return result
    except Exception as e:
        log("ORDER ERROR: " + str(e))
        return {}

def get_current_position_size():
    try:
        positions = client.get_positions(category="linear", symbol=SYMBOL)
        for p in positions["result"]["list"]:
            if float(p["size"]) > 0:
                return float(p["size"]), p["side"]
        return 0, None
    except Exception as e:
        log("ERROR obteniendo posicion: " + str(e))
        return 0, None

def add_margin_from_profit():
    with state_lock:
        profit = state.get("last_tp_profit", 0)
        if profit <= 0:
            log("Sin profits para reinyectar")
            return
        price = get_current_price()
        if price is None:
            return
        size = int(round(profit * LEVERAGE / price))
        if size < 1:
            log("Margen muy pequeno para agregar: $" + str(profit))
            return
        side = "Buy" if state["position"] == "long" else "Sell"
        try:
            client.place_order(category="linear", symbol=SYMBOL, side=side, orderType="Market", qty=str(size))
            log("MARGIN ADD OK - $" + str(profit) + " → " + str(size))
            state["last_tp_profit"] = 0.0
            save_state(state)
        except Exception as e:
            log("MARGIN ADD ERROR: " + str(e))

def execute_tp_daily():
    with state_lock:
        if state["tp_daily_done"]:
            log("TP Daily ya ejecutado - ignorando")
            return
        if not is_in_profit():
            log("TP Daily ignorado - posicion en perdida o break even")
            return
        current_size, side = get_current_position_size()
        if current_size == 0:
            log("No hay posicion abierta para TP Daily")
            return
        tp_size = int(round(current_size * TP_DAILY_PCT))
        if tp_size < 1:
            log("TP Daily ignorado - size muy pequeno")
            return
        close_side = "Sell" if side == "Buy" else "Buy"
        try:
            price = get_current_price()
            client.place_order(category="linear", symbol=SYMBOL, side=close_side, orderType="Market", qty=str(tp_size), reduceOnly=True)
            entry = float(str(state["entry_price"]))
            if state["position"] == "long":
                profit = round(tp_size * (price - entry), 2)
            else:
                profit = round(tp_size * (entry - price), 2)
            if profit > 0:
                state["last_tp_profit"] = profit
            state["tp_daily_done"] = True
            save_state(state)
            log("TP Daily OK - cerrado " + str(tp_size) + " (75%) - Profit: $" + str(profit))
        except Exception as e:
            log("TP Daily ERROR: " + str(e))

def reset_tp_flags():
    state["last_tp_profit"] = 0.0
    state["tp_daily_done"] = False

def process_signal(data):
    action = data.get("action")
    log("Procesando: " + str(action))

    if action == "long":
        with state_lock:
            if state["position"] == "long":
                if state["last_tp_profit"] > 0:
                    log("LONG repetido - agregando margen de profits")
                    # salimos del lock antes de llamar add_margin
                else:
                    log("LONG repetido - sin profits para agregar")
                    return
        if state["position"] == "long":
            if state["last_tp_profit"] > 0:
                add_margin_from_profit()
            return
        log("LONG SIGNAL @ " + str(data.get("price")))
        close_all()
        time.sleep(2)
        set_leverage()
        time.sleep(1)
        size = get_order_size()
        place_order("Buy", size)
        with state_lock:
            state["position"] = "long"
            state["entry_price"] = float(str(data.get("price")))
            reset_tp_flags()
            save_state(state)

    elif action == "short":
        with state_lock:
            if state["position"] == "short":
                if state["last_tp_profit"] > 0:
                    log("SHORT repetido - agregando margen de profits")
                else:
                    log("SHORT repetido - sin profits para agregar")
                    return
        if state["position"] == "short":
            if state["last_tp_profit"] > 0:
                add_margin_from_profit()
            return
        log("SHORT SIGNAL @ " + str(data.get("price")))
        close_all()
        time.sleep(2)
        set_leverage()
        time.sleep(1)
        size = get_order_size()
        place_order("Sell", size)
        with state_lock:
            state["position"] = "short"
            state["entry_price"] = float(str(data.get("price")))
            reset_tp_flags()
            save_state(state)

    elif action == "tp_daily_rsi":
        if state["position"] is None:
            log("TP Daily ignorado - sin posicion")
            return
        execute_tp_daily()

    else:
        log("Accion desconocida: " + str(action))

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    log("Signal recibida: " + str(data))
    threading.Thread(target=process_signal, args=(data,), daemon=True).start()
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    print("MWhalekiller Bot Bybit - Daily v1")
    print("Par: " + SYMBOL + " | Leverage: " + str(LEVERAGE) + "x | Margin/trade: $" + str(MARGIN_PER_TRADE))
    print("Entrada: MWhale Daily | TP: 75% RSI Daily cruce | Resto 25% corre libre")
    print("Margin add: reinyecta profits del TP cuando señal repetida")
    print("Fix: webhook responde inmediatamente, ejecuta en thread separado")
    print("Estado actual: " + str(state))
    app.run(host="0.0.0.0", port=5000)
