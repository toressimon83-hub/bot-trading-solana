
import os
import time
import requests
import sqlite3
import warnings
import hmac
import hashlib
import threading
from datetime import datetime
from flask import Flask

# Servidor web integrado para evitar que el hosting apague el bot
app = Flask('')

@app.route('/')
def home():
    return "Bot de Trading Activo"

def run():
    puerto = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=puerto)

t = threading.Thread(target=run)
t.start()

warnings.filterwarnings("ignore", category=UserWarning)

# --- 1. CONFIGURACIÓN GENERAL ---
MODO_TESTNET = True       # True = Cuenta de simulación | False = Dinero REAL
TF_MINUTOS = 15    
SIMBOLO = 'SOLUSDT'       # Binance sin barras: SOLUSDT
LIMIT_VELAS = 100 
PORCENTAJE_RIESGO = 0.02  
APALANCAMIENTO = 10
BASE_DATOS = "auditoria_bot.db"

# URLs Oficiales de Binance Futuros
API_URL = "https://binancefuture.com" if MODO_TESTNET else "https://binance.com"

API_KEY = os.environ.get('BINANCE_API_KEY', 'TU_API_KEY_AQUI')
SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY', 'TU_SECRET_KEY_AQUI')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'TU_TOKEN_DE_TELEGRAM_AQUI')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'TU_CHAT_ID_AQUI')

MAX_PERDIDAS_DIARIAS = 3
PERDIDAS_HOY = 0
FECHA_ACTUAL = datetime.now().date()
db_lock = threading.Lock()

# --- 2. MOTOR DE FIRMA DE SEGURIDAD ---
def firmar_peticion(query_string):
    return hmac.new(SECRET_KEY.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

def enviar_peticion_binance(metodo, endpoint, parametros={}):
    parametros['timestamp'] = int(time.time() * 1000)
    query = "&".join([f"{k}={v}" for k, v in parametros.items()])
    query += f"&signature={firmar_peticion(query)}"
    headers = {"X-MBX-APIKEY": API_KEY}
    url = f"{API_URL}{endpoint}?{query}"
    try:
        if metodo.upper() == "POST":
            res = requests.post(url, headers=headers, timeout=5)
        elif metodo.upper() == "DELETE":
            res = requests.delete(url, headers=headers, timeout=5)
        else:
            res = requests.get(url, headers=headers, timeout=5)
        return res.json()
    except Exception as e:
        print(f"Error de red con Binance: {e}")
        return None
# --- 3. MATEMÁTICAS ADAPTATIVAS ---
def calcular_rsi_rapido(precios, periodo=14):
    if len(precios) < periodo + 1: return 50
    ganancias, perdidas = [], []
    for i in range(1, len(precios)):
        diff = precios[i] - precios[i-1]
        ganancias.append(max(diff, 0))
        perdidas.append(max(-diff, 0))
    avg_gain = sum(ganancias[:periodo]) / periodo
    avg_loss = sum(perdidas[:periodo]) / periodo
    for i in range(periodo, len(ganancias)):
        avg_gain = (avg_gain * 13 + ganancias[i]) / 14
        avg_loss = (avg_loss * 13 + perdidas[i]) / 14
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calcular_ema_rapida(precios, periodo):
    if len(precios) < periodo: return precios[-1]
    k = 2 / (periodo + 1)
    ema = precios
    for p in precios[1:]: 
        ema = (p * k) + (ema * (1 - k))
    return ema

def calcular_atr_rapido(velas, periodo=14):
    if len(velas) < periodo + 1: return 1.0
    trs = []
    for i in range(1, len(velas)):
        h, l, c_prev = float(velas[i][2]), float(velas[i][3]), float(velas[i][4])
        trs.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
    return sum(trs[-periodo:]) / periodo

# --- 4. BASE DE DATOS Y TELEGRAM ---
def inicializar_base_de_datos():
    with db_lock:
        conexion = sqlite3.connect(BASE_DATOS)
        conexion.cursor().execute('''
            CREATE TABLE IF NOT EXISTS operaciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT, tipo TEXT, 
                precio_entrada REAL, cantidad REAL, tp REAL, sl REAL, estado TEXT
            )
        ''')
        conexion.commit()
        conexion.close()

def registrar_operacion_db(tipo, precio, cantidad, tp, sl):
    with db_lock:
        try:
            conexion = sqlite3.connect(BASE_DATOS)
            conexion.cursor().execute('''
                INSERT INTO operaciones (fecha, tipo, precio_entrada, cantidad, tp, sl, estado)
                VALUES (?, ?, ?, ?, ?, ?, 'ABIERTA')
            ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), tipo, precio, cantidad, tp, sl))
            conexion.commit()
            conexion.close()
        except Exception: pass

def enviar_mensaje_telegram(mensaje):
    try:
        url = f"https://telegram.org{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}, timeout=2) 
    except Exception: pass
# --- 5. ESTRATEGIA Y GESTIÓN DE POSICIONES ---
def obtener_posicion_activa():
    res = enviar_peticion_binance("GET", "/fapi/v2/positionRisk", {"symbol": SIMBOLO})
    if res and isinstance(res, list):
        for p in res:
            if p.get('symbol') == SIMBOLO:
                return p
    return None

def limpiar_todas_las_ordenes():
    enviar_peticion_binance("DELETE", "/fapi/v1/allOpenOrders", {"symbol": SIMBOLO})

def auditar_y_gestionar_salidas():
    try:
        pos = obtener_posicion_activa()
        if not pos: return
        contratos = abs(float(pos.get('positionAmt', 0)))
        
        if contratos == 0:
            with db_lock:
                conexion = sqlite3.connect(BASE_DATOS)
                cursor = conexion.cursor()
                cursor.execute("SELECT id FROM operaciones WHERE estado IN ('ABIERTA', 'PARCIAL')")
                op = cursor.fetchone()
                if op:
                    cursor.execute("UPDATE operaciones SET estado = 'CERRADA' WHERE id = ?", (op[0],))
                    conexion.commit()
                    enviar_mensaje_telegram("🏁 *Operación Finalizada en Binance*")
                conexion.close()
            return

        precio_marca = float(pos['markPrice'])
        with db_lock:
            conexion = sqlite3.connect(BASE_DATOS)
            cursor = conexion.cursor()
            cursor.execute("SELECT id, tipo, precio_entrada, sl, tp, estado FROM operaciones WHERE estado = 'ABIERTA'")
            op_local = cursor.fetchone()
            conexion.close()

        if op_local:
            op_id, tipo, entrada, sl, tp, estado = op_local
            distancia = abs(entrada - sl)
            ejecutar_parcial = False
            
            if tipo == "COMPRA" and precio_marca >= (entrada + distancia): ejecutar_parcial = True
            if tipo == "VENTA" and precio_marca <= (entrada - distancia): ejecutar_parcial = True
            
            if ejecutar_parcial:
                with db_lock:
                    conexion = sqlite3.connect(BASE_DATOS)
                    conexion.cursor().execute("UPDATE operaciones SET estado = 'PARCIAL' WHERE id = ?", (op_id,))
                    conexion.commit()
                    conexion.close()
                
                limpiar_todas_las_ordenes()
                lado_cierre = "SELL" if tipo == "COMPRA" else "BUY"
                
                enviar_peticion_binance("POST", "/fapi/v1/order", {
                    "symbol": SIMBOLO, "side": lado_cierre, "type": "MARKET", "quantity": round(contratos / 2, 2), "reduceOnly": "true"
                })
                enviar_peticion_binance("POST", "/fapi/v1/order", {
                    "symbol": SIMBOLO, "side": lado_cierre, "type": "STOP_MARKET", "stopPrice": round(entrada, 2), "quantity": round(contratos / 2, 2), "reduceOnly": "true"
                })
                enviar_mensaje_telegram(f"💰 *Take Profit Parcial Ejecutado*. Posición asegurada en Breakeven.")
    except Exception as e:
        print(f"Error en auditoría: {e}")

def procesar_estrategia():
    try:
        pos = obtener_posicion_activa()
        if pos and abs(float(pos.get('positionAmt', 0))) > 0: return

        url_velas = f"{API_URL}/fapi/v1/klines?symbol={SIMBOLO}&interval={TF_MINUTOS}m&limit={LIMIT_VELAS}"
        velas = requests.get(url_velas, timeout=5).json()
        closes = [float(v[4]) for v in velas]
        precio_actual = closes[-1]
        
        rsi = calcular_rsi_rapido(closes)
        ema50 = calcular_ema_rapida(closes, 50)
        atr = calcular_atr_rapido(velas)
        
        direccion = None
        if rsi < 35 and precio_actual > ema50: direccion = "COMPRA"
        elif rsi > 65 and precio_actual < ema50: direccion = "VENTA"
        
        if direccion:
            limpiar_todas_las_ordenes()
            res_bal = enviar_peticion_binance("GET", "/fapi/v2/balance")
            saldo_usdt = 100.0
            if isinstance(res_bal, list):
                for b in res_bal:
                    if b.get('asset') == 'USDT':
                        saldo_usdt = float(b['balance'])
            
            distancia_sl = atr * 2.0
            sl = precio_actual - distancia_sl if direccion == "COMPRA" else precio_actual + distancia_sl
            tp = precio_actual + (distancia_sl * 2.0) if direccion == "COMPRA" else precio_actual - (distancia_sl * 2.0)
            
            cant = (saldo_usdt * PORCENTAJE_RIESGO) / distancia_sl
            lado = "BUY" if direccion == "COMPRA" else "SELL"
            lado_c = "SELL" if lado == "BUY" else "BUY"
            
            enviar_peticion_binance("POST", "/fapi/v1/order", {"symbol": SIMBOLO, "side": lado, "type": "MARKET", "quantity": round(cant, 2)})
            enviar_peticion_binance("POST", "/fapi/v1/order", {"symbol": SIMBOLO, "side": lado_c, "type": "STOP_MARKET", "stopPrice": round(sl, 2), "quantity": round(cant, 2), "reduceOnly": "true"})
            
            registrar_operacion_db(direccion, precio_actual, cant, tp, sl)
            enviar_mensaje_telegram(f"🚀 *Operación Abierta*: {direccion}\n• Entrada: {precio_actual}\n• SL: {round(sl,2)}")
    except Exception as e:
        print(f"Error analizando señales: {e}")

# --- 6. BUCLE PRINCIPAL MULTI-HILOS ---
def hilo_monitoreo():
    while True:
        auditar_y_gestionar_salidas()
        time.sleep(5)

def main():
    inicializar_base_de_datos()
    print("🤖 Bot Ultraligero Iniciado. Monitoreando mercado...")
    enviar_peticion_binance("POST", "/fapi/v1/leverage", {"symbol": SIMBOLO, "leverage": APALANCAMIENTO})
    threading.Thread(target=hilo_monitoreo, daemon=True).start()
    while True:
        ahora = datetime.now()
        segs_espera = ((TF_MINUTOS - (ahora.minute % TF_MINUTOS)) * 60) - ahora.second + 3.0
        time.sleep(max(segs_espera, 1))
        procesar_estrategia()

if __name__ == "__main__":
    main()
