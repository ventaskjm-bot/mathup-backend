"""
MATHUP Backend - Sistema de pagos Premium
Owner: Mateo Martínez
Recibe webhooks de Mercado Pago y genera códigos de activación
"""

import os
import json
import uuid
import hashlib
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)

# CORS: permite que el HTML (GitHub Pages) llame a este backend
CORS(app, origins=[
    "https://*.github.io",
    "http://localhost",
    "http://127.0.0.1",
    "null"  # para abrir el HTML directamente desde archivo
])

# ══════════════════════════════════════════════
# CONFIGURACIÓN — Variables de entorno en Render
# ══════════════════════════════════════════════
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "")        # Tu token de MP producción
MP_WEBHOOK_SECRET = os.environ.get("MP_WEBHOOK_SECRET", "")    # Secret para verificar webhook (opcional)
ADMIN_KEY = os.environ.get("ADMIN_KEY", "mathup-admin-2025")   # Clave para ver códigos desde panel

# ══════════════════════════════════════════════
# BASE DE DATOS — Archivo JSON local
# En Render el disco persiste mientras el servicio esté activo
# Para producción seria → migrar a Supabase (gratis hasta 500MB)
# ══════════════════════════════════════════════
DB_FILE = "db.json"

def load_db():
    """Carga la base de datos desde archivo JSON"""
    if not os.path.exists(DB_FILE):
        return {
            "codes": {},        # codigo -> {status, payment_id, buyer_name, created_at, used_at}
            "payments": {},     # payment_id -> {status, amount, buyer, code, created_at}
            "stats": {
                "total_payments": 0,
                "total_activations": 0,
                "revenue": 0
            }
        }
    with open(DB_FILE, "r") as f:
        return json.load(f)

def save_db(db):
    """Guarda la base de datos en archivo JSON"""
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2, ensure_ascii=False, default=str)

def generate_code():
    """Genera un código único tipo MATHUP-XXXX-XXXX"""
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    import random
    while True:
        part1 = "".join(random.choices(chars, k=4))
        part2 = "".join(random.choices(chars, k=4))
        code = f"MATHUP-{part1}-{part2}"
        db = load_db()
        if code not in db["codes"]:
            return code

def verify_mp_payment(payment_id):
    """Verifica con la API de MP que el pago es real y fue aprobado"""
    if not MP_ACCESS_TOKEN:
        # Modo desarrollo sin token — simular respuesta
        return {
            "valid": True,
            "status": "approved",
            "amount": 1000,
            "buyer_name": "Usuario Test",
            "buyer_email": "test@test.com",
            "payment_id": payment_id
        }

    try:
        url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
        headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code != 200:
            return {"valid": False, "error": f"MP API error: {response.status_code}"}

        data = response.json()
        return {
            "valid": True,
            "status": data.get("status"),
            "amount": data.get("transaction_amount", 0),
            "buyer_name": data.get("payer", {}).get("first_name", "") + " " + data.get("payer", {}).get("last_name", ""),
            "buyer_email": data.get("payer", {}).get("email", ""),
            "payment_id": payment_id
        }
    except Exception as e:
        return {"valid": False, "error": str(e)}

# ══════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "app": "MATHUP Backend",
        "version": "1.0",
        "status": "online",
        "owner": "Mateo Martínez"
    })

@app.route("/health", methods=["GET"])
def health():
    db = load_db()
    return jsonify({
        "status": "ok",
        "total_codes": len(db["codes"]),
        "total_payments": db["stats"]["total_payments"],
        "total_activations": db["stats"]["total_activations"]
    })

# ── WEBHOOK DE MERCADO PAGO ──────────────────
@app.route("/webhook/mp", methods=["POST"])
def webhook_mp():
    """
    Mercado Pago llama aquí automáticamente cuando hay un pago.
    Configurar en: https://www.mercadopago.com.ar/developers/panel/notifications
    URL a poner: https://TU-APP.onrender.com/webhook/mp
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        print(f"[WEBHOOK] Recibido: {json.dumps(data)}")

        # MP envía tipo "payment" cuando es un pago
        topic = data.get("type") or request.args.get("topic", "")
        resource_id = data.get("data", {}).get("id") or request.args.get("id", "")

        if not resource_id:
            return jsonify({"status": "ignored", "reason": "no id"}), 200

        if topic not in ["payment", "merchant_order"]:
            return jsonify({"status": "ignored", "reason": f"topic={topic}"}), 200

        # Verificar el pago con la API de MP
        payment_info = verify_mp_payment(resource_id)

        if not payment_info["valid"]:
            print(f"[WEBHOOK] Pago inválido: {payment_info}")
            return jsonify({"status": "error", "detail": payment_info.get("error")}), 200

        if payment_info["status"] != "approved":
            print(f"[WEBHOOK] Pago no aprobado: {payment_info['status']}")
            return jsonify({"status": "ignored", "reason": "not approved"}), 200

        # Verificar monto mínimo ($900 para tolerar variaciones de comisión)
        if payment_info["amount"] < 900:
            print(f"[WEBHOOK] Monto insuficiente: {payment_info['amount']}")
            return jsonify({"status": "ignored", "reason": "amount too low"}), 200

        db = load_db()
        payment_id = str(resource_id)

        # Evitar procesar el mismo pago dos veces
        if payment_id in db["payments"]:
            existing_code = db["payments"][payment_id].get("code")
            print(f"[WEBHOOK] Pago ya procesado, código: {existing_code}")
            return jsonify({"status": "already_processed", "code": existing_code}), 200

        # Generar código único
        code = generate_code()
        now = datetime.utcnow().isoformat()
        buyer_name = payment_info["buyer_name"].strip() or "Cliente"

        # Guardar en DB
        db["codes"][code] = {
            "status": "active",
            "payment_id": payment_id,
            "buyer_name": buyer_name,
            "buyer_email": payment_info["buyer_email"],
            "amount": payment_info["amount"],
            "created_at": now,
            "used_at": None,
            "used_device": None
        }
        db["payments"][payment_id] = {
            "status": "approved",
            "amount": payment_info["amount"],
            "buyer": buyer_name,
            "buyer_email": payment_info["buyer_email"],
            "code": code,
            "created_at": now
        }
        db["stats"]["total_payments"] += 1
        db["stats"]["revenue"] = db["stats"].get("revenue", 0) + payment_info["amount"]
        save_db(db)

        print(f"[WEBHOOK] ✅ Código generado: {code} para {buyer_name}")
        return jsonify({"status": "ok", "code": code}), 200

    except Exception as e:
        print(f"[WEBHOOK] Error: {e}")
        return jsonify({"status": "error", "detail": str(e)}), 500

# ── CONSULTAR CÓDIGO POR PAYMENT_ID ──────────
@app.route("/code/<payment_id>", methods=["GET"])
def get_code(payment_id):
    """
    El frontend llama aquí para obtener el código después del pago.
    MP redirige al usuario a una URL de retorno donde el frontend hace esta consulta.
    """
    db = load_db()
    payment = db["payments"].get(str(payment_id))

    if not payment:
        return jsonify({"status": "pending", "message": "Pago no procesado aún"}), 200

    if payment["status"] != "approved":
        return jsonify({"status": "rejected", "message": "Pago no aprobado"}), 200

    code = payment.get("code")
    code_data = db["codes"].get(code, {})

    return jsonify({
        "status": "approved",
        "code": code,
        "buyer": payment["buyer"],
        "already_used": code_data.get("used_at") is not None
    }), 200

# ── VALIDAR CÓDIGO (llamado por la app al activar) ──
@app.route("/validate", methods=["POST"])
def validate_code():
    """
    La app llama aquí para validar que un código existe y no fue usado.
    """
    data = request.get_json(force=True, silent=True) or {}
    code = data.get("code", "").strip().upper()
    device_id = data.get("device_id", "unknown")

    if not code:
        return jsonify({"valid": False, "error": "Código vacío"}), 200

    import re
    if not re.match(r'^MATHUP-[A-Z0-9]{4}-[A-Z0-9]{4}$', code):
        return jsonify({"valid": False, "error": "Formato inválido"}), 200

    db = load_db()

    if code not in db["codes"]:
        return jsonify({"valid": False, "error": "Código no existe"}), 200

    code_data = db["codes"][code]

    if code_data["status"] != "active":
        return jsonify({"valid": False, "error": "Código inactivo"}), 200

    # Permitir el mismo device usar su mismo código (por si reinstala)
    if code_data["used_at"] and code_data["used_device"] != device_id:
        return jsonify({"valid": False, "error": "Código ya utilizado en otro dispositivo"}), 200

    # Marcar como usado
    db["codes"][code]["used_at"] = datetime.utcnow().isoformat()
    db["codes"][code]["used_device"] = device_id
    db["stats"]["total_activations"] = db["stats"].get("total_activations", 0) + 1
    save_db(db)

    return jsonify({
        "valid": True,
        "buyer": code_data["buyer_name"],
        "expires_days": 30
    }), 200

# ── PANEL ADMIN ──────────────────────────────
@app.route("/admin", methods=["GET"])
def admin_panel():
    """Panel simple para ver estado del sistema"""
    key = request.args.get("key", "")
    if key != ADMIN_KEY:
        return jsonify({"error": "No autorizado"}), 403

    db = load_db()
    codes_list = []
    for code, data in db["codes"].items():
        codes_list.append({
            "code": code,
            "buyer": data["buyer_name"],
            "amount": data["amount"],
            "created": data["created_at"][:10],
            "used": data["used_at"][:10] if data["used_at"] else "No usado",
        })

    codes_list.sort(key=lambda x: x["created"], reverse=True)

    return jsonify({
        "stats": db["stats"],
        "recent_codes": codes_list[:50],
        "total_codes_generated": len(codes_list)
    })

# ── SIMULADOR DE PAGO (solo para testing) ────
@app.route("/test/simulate_payment", methods=["POST"])
def simulate_payment():
    """
    SOLO PARA PRUEBAS — simula un pago aprobado.
    Deshabilitar en producción poniendo ENABLE_TEST_ENDPOINT=false en Render.
    """
    if os.environ.get("ENABLE_TEST_ENDPOINT", "true").lower() == "false":
        return jsonify({"error": "Endpoint deshabilitado en producción"}), 403

    data = request.get_json(force=True, silent=True) or {}
    buyer_name = data.get("buyer_name", "Usuario de Prueba")
    fake_payment_id = f"TEST-{uuid.uuid4().hex[:8].upper()}"

    db = load_db()
    code = generate_code()
    now = datetime.utcnow().isoformat()

    db["codes"][code] = {
        "status": "active",
        "payment_id": fake_payment_id,
        "buyer_name": buyer_name,
        "buyer_email": "test@mathup.com",
        "amount": 1000,
        "created_at": now,
        "used_at": None,
        "used_device": None
    }
    db["payments"][fake_payment_id] = {
        "status": "approved",
        "amount": 1000,
        "buyer": buyer_name,
        "buyer_email": "test@mathup.com",
        "code": code,
        "created_at": now
    }
    db["stats"]["total_payments"] += 1
    save_db(db)

    return jsonify({
        "status": "ok",
        "message": "Pago simulado exitosamente",
        "payment_id": fake_payment_id,
        "code": code
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
