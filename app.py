import os, json, re, traceback, time, urllib.request
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
from google import genai
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="")
app.logger.setLevel("DEBUG")

gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

SHEET_ID = os.getenv("SHEET_ID")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

MESES = ["Enero","Febrero","Marzo","Abril","Mayo","Junio",
         "Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]

CATEGORIAS = {
    "Alimentación": ["Supermercado","Restaurante","Delivery","Cafetería","Kiosco"],
    "Transporte":   ["Uber","Taxi","SUBE","Combustible","Peaje","Estacionamiento"],
    "Entretenimiento": ["Streaming","Cine","Salidas","Juegos","Libros"],
    "Salud":        ["Farmacia","Médico","Gimnasio","Óptica"],
    "Ropa":         ["Ropa","Calzado","Accesorios"],
    "Hogar":        ["Alquiler","Expensas","Servicios","Limpieza","Muebles"],
    "Trabajo":      ["Software","Hardware","Coworking","Capacitación"],
    "Otro":         ["Regalo","Donación","Impuestos","Otro"],
}

# ── Cotización del dólar (blue) con cache de 1 hora ──
DOLAR_URL = "https://dolarapi.com/v1/dolares/blue"
_dolar_cache = {"rate": None, "ts": 0, "fecha": None}

def get_dolar_blue():
    now = time.time()
    if _dolar_cache["rate"] and (now - _dolar_cache["ts"]) < 3600:
        return _dolar_cache["rate"], _dolar_cache["fecha"]
    with urllib.request.urlopen(DOLAR_URL, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    rate = float(data["venta"])
    _dolar_cache.update(rate=rate, ts=now, fecha=data.get("fechaActualizacion"))
    return rate, _dolar_cache["fecha"]

def get_sheets_service():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        import json as _json
        info = _json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = service_account.Credentials.from_service_account_file(
            os.getenv("GOOGLE_CREDENTIALS", "credentials.json"), scopes=SCOPES
        )
    return build("sheets", "v4", credentials=creds)

def get_or_create_month_sheet(service, year, month):
    sheet_name = f"{MESES[month-1]} {year}"
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    existing = [s["properties"]["title"] for s in meta["sheets"]]

    if sheet_name not in existing:
        body = {"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]}
        service.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body=body).execute()

        headers = [["Fecha","Descripción","Monto","Categoría","Tipo","Nota"]]
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{sheet_name}'!A1:F1",
            valueInputOption="RAW",
            body={"values": headers}
        ).execute()

    return sheet_name

def append_expense(expense):
    service = get_sheets_service()
    now = datetime.now()
    date_str = expense.get("fecha") or now.strftime("%d/%m/%Y")

    try:
        parts = date_str.split("/")
        day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
    except Exception:
        day, month, year = now.day, now.month, now.year

    sheet_name = get_or_create_month_sheet(service, year, month)

    row = [[
        f"{day:02d}/{month:02d}/{year}",
        expense.get("descripcion", ""),
        expense.get("monto", 0),
        expense.get("categoria", "Otro"),
        expense.get("tipo", "Otro"),
        expense.get("nota", ""),
    ]]

    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"'{sheet_name}'!A:F",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": row}
    ).execute()

    return sheet_name

PROMPT_TEMPLATE = """Sos un asistente que extrae datos de gastos del texto del usuario.
Hoy es {today}.

El usuario dijo: "{text}"

Extraé los datos y devolvé SOLO un JSON válido con esta estructura exacta:
{{
  "fecha": "DD/MM/YYYY",
  "descripcion": "texto descriptivo breve",
  "monto": 1234,
  "moneda": "ARS o USD",
  "categoria": "una de: Alimentación, Transporte, Entretenimiento, Salud, Ropa, Hogar, Trabajo, Otro",
  "tipo": "subtipo específico",
  "nota": "detalle extra si hay, sino vacío"
}}

Reglas:
- Si no menciona fecha, usá hoy: {today}
- Si dice "ayer" restá 1 día, "el lunes" calculá el lunes más reciente, etc.
- El monto debe ser un número sin símbolos, en la moneda original que dijo el usuario
- "moneda" es USD si menciona dólares, dolares, usd, u$s, o "verdes"; en cualquier otro caso es ARS
- La categoría debe ser exactamente una de las listadas
- El tipo debe ser específico (Supermercado, Uber, Netflix, etc.)
- No incluyas nada más que el JSON"""

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/favicon.ico")
def favicon():
    return send_from_directory("static", "icon-192.png", mimetype="image/png")

@app.route("/config")
def config():
    return jsonify({
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
    })

@app.route("/parse", methods=["POST"])
def parse_expense():
    data = request.get_json()
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Texto vacío"}), 400

    today = datetime.now().strftime("%d/%m/%Y")
    prompt = PROMPT_TEMPLATE.format(text=text, today=today)

    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt
        )
        raw = response.text.strip()
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        expense = json.loads(raw)

        # Conversión USD -> ARS con dólar blue
        if str(expense.get("moneda", "ARS")).upper() == "USD":
            usd = float(expense["monto"])
            rate, _ = get_dolar_blue()
            ars = round(usd * rate)
            extra = f"USD {usd:g} @ ${rate:g} (blue)"
            expense["monto"] = ars
            expense["nota"] = (expense.get("nota", "") + " · " + extra).strip(" ·")
            expense["conversion"] = {"usd": usd, "rate": rate, "ars": ars}
        expense["moneda"] = "ARS"

        return jsonify({"ok": True, "expense": expense})
    except Exception as e:
        tb = traceback.format_exc()
        app.logger.error(tb)
        return jsonify({"error": str(e), "trace": tb}), 500

@app.route("/save", methods=["POST"])
def save_expense():
    data = request.get_json()
    expense = data.get("expense")
    if not expense:
        return jsonify({"error": "Sin datos"}), 400

    try:
        sheet_name = append_expense(expense)
        return jsonify({"ok": True, "sheet": sheet_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
