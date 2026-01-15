import logging
import pandas as pd
import pytesseract
import re
from PIL import Image
from io import BytesIO
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import os

# Ruta de Tesseract
#pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Token
#TOKEN = "TOQUEN LOCAL"
TOKEN=os.getenv("BOT_TOKEN")

# Google Sheet
SHEET_URL = "https://docs.google.com/spreadsheets/d/1MKT4wK52AznGAhhD04i5xABK3QZs3_otstdChUmC_t4/export?format=csv"

logging.basicConfig(level=logging.INFO)

# Cargar sheet
def load_db():
    df = pd.read_csv(SHEET_URL, header=None)
    return df.astype(str)

# /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hola, soy el verificador de pagos\n\n"
        "📸 Envíame una captura de Yape, Plin o BCP y validaré el pago."
    )

# Detectar procedencia
def detectar_origen(text):
    t = text.lower()
    if "yape" in t:
        return "YAPE"
    if "plin" in t:
        return "PLIN"
    if "bcp" in t or "bbva" in t:
        return "BCP"
    return "DESCONOCIDO"

# Foto
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    file = await photo.get_file()
    img_bytes = await file.download_as_bytearray()
    img = Image.open(BytesIO(img_bytes))

    text = pytesseract.image_to_string(img)
    clean = text.lower()

    # 🏦 Procedencia
    origen = detectar_origen(text)

    # 💰 Monto
    monto = None

    # Caso especial: "importe enviado"
    m_imp = re.search(r"importe enviado\s*s?\/?\s*([0-9]+(?:[.,][0-9]{2})?)", clean)
    if m_imp:
        monto = m_imp.group(1).replace(",", ".")
    else:
        # Normal Yape / BCP / Plin
        m = re.search(r"s\/\s*([0-9]+(?:[.,][0-9]{2})?)", clean)
        if m:
            monto = m.group(1).replace(",", ".")

    # ⏰ Hora
    hora = None
    m = re.search(r"([0-9]{1,2}:[0-9]{2})", clean)
    if m:
        hora = m.group(1)

    # 📅 Fecha
    fecha = None

    m1 = re.search(r"([0-9]{1,2}\s\w+\.?\s[0-9]{4})", clean)
    m2 = re.search(r"([0-9]{1,2}\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\s+[0-9]{4})", clean)
    m3 = re.search(r"(domingo|lunes|martes|miércoles|jueves|viernes|sábado)\s+([0-9]{1,2}\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\s+[0-9]{4})", clean)

    if m3:
        fecha = m3.group(2)
    elif m2:
        fecha = m2.group(1)
    elif m1:
        fecha = m1.group(1)

    # 🧾 Número de operación (modo seguro para Yape primero)
    operacion = None

    # 1️⃣ PRIMER INTENTO → solo números largos (YAPE real)
    numeros = re.findall(r"\b[0-9]{7,12}\b", clean)
    if numeros:
        operacion = max(numeros, key=len)

    # 2️⃣ SI NO ENCONTRÓ NADA → intentar formato PLIN / BCP
    if not operacion:
        candidatos = re.findall(r"(?:de operaci[oó]n|nro\.?|número)\s*[:\-]?\s*([a-z0-9]{6,15})", clean)
        if candidatos:
            operacion = candidatos[0]
        else:
            mix = re.findall(r"\b[a-z0-9]{8,15}\b", clean)
            if mix:
                operacion = max(mix, key=len)

    # 🔐 Código de seguridad = últimos 3 dígitos
    codigo = None
    if operacion:
        codigo = operacion[-3:]


    # ===============================
    # 🔐 VALIDACIÓN REAL CON SHEET
    # ===============================

    pago_valido = False

    # Si el código OCR tiene letras → PLIN / BCP → ignorar código
    ignorar_codigo = bool(codigo and re.search(r"[a-z]", codigo.lower()))

    try:
        db = load_db()

        for i, row in db.iterrows():
            fila = " ".join(row.astype(str)).lower()

            # Normalizar acentos
            fila = fila.replace("á","a").replace("é","e").replace("í","i").replace("ó","o").replace("ú","u")

            # ===========================
            # VALIDACIONES
            # ===========================

            # Código
            if ignorar_codigo:
                ok_codigo = True
            else:
                ok_codigo = codigo and re.search(rf"\b{codigo}\b", fila)

            # Monto
            ok_monto = monto and monto.replace(".", "").replace(",", "") in fila.replace(".", "").replace(",", "")

            # Hora
            ok_hora = False
            if hora:
                # ejemplo fila: 14/1/2026 16:26:44
                m = re.search(r"(\d{1,2}:\d{2})(?::\d{2})?", fila)
                if m:
                    hora_db = m.group(1)  # 16:26

                    if hora == hora_db:
                        ok_hora = True
                    else:
                        # convertir OCR 12h → 24h
                        h, m2 = hora.split(":")
                        h = int(h)
                        h_pm = (h + 12) % 24
                        if f"{h_pm:02d}:{m2}" == hora_db:
                            ok_hora = True

            # Fecha
            ok_fecha = False
            fecha_normal = None
            fecha_alt = None

            if fecha:
                meses = {
                    "ene": "01","feb": "02","mar": "03","abr": "04","may": "05","jun": "06",
                    "jul": "07","ago": "08","sep": "09","oct": "10","nov": "11","dic": "12"
                }

                f = fecha.lower()

                for k in meses:
                    if k in f:
                        dia = re.search(r"\d{1,2}", f).group()
                        dia2 = str(int(dia))      # sin cero → 14
                        anio = re.search(r"\d{4}", f).group()
                        mes = meses[k]

                        fecha_normal = f"{dia.zfill(2)}/{mes}/{anio}"   # 14/01/2026
                        fecha_alt    = f"{dia2}/{int(mes)}/{anio}"     # 14/1/2026

                        if fecha_normal in fila or fecha_alt in fila:
                            ok_fecha = True


            # ===========================
            # 🔎 DEBUG FORENSE
            # ===========================

            print("\n---------------- FILA", i, "----------------")
            print("FILA:", fila)
            print("BUSCO:")
            print("  CODIGO:", codigo, "(ignorar:", ignorar_codigo, ") ->", ok_codigo)
            print("  MONTO :", monto, "->", ok_monto)
            print("  HORA  :", hora, "->", ok_hora)
            print("  FECHA :", fecha, "->", fecha_normal, "->", ok_fecha)

            # ===========================
            # MATCH REAL
            # ===========================

            if ok_codigo and ok_monto and ok_hora and ok_fecha:
                print(">>> MATCH EN FILA", i)
                pago_valido = True
                break

    except Exception as e:
        print("Error validando:", e)


    # ===============================
    # RESULTADO FINAL
    # ===============================

    if pago_valido:
        if ignorar_codigo:
            resultado = "✅ PAGO CONFIRMADO\nℹ️ Se ignoró el código de seguridad porque entre PLIN y YAPE no coincide necesariamente"
        else:
            resultado = "✅ PAGO CONFIRMADO POR SISTEMA BANCARIO"
    else:
        resultado = "❌ No se encontró registro válido\n👨‍💼 Consultar con el jefe"


    reply = f"""
💳 Leyendo {origen}

💰 Monto: S/ {monto or "?"}
📅 Fecha: {fecha or "?"}
⏰ Hora: {hora or "?"}
🔐 Código de seguridad: {codigo or "?"}
🧾 Operación: {operacion or "?"}

{resultado}
"""

    await update.message.reply_text(reply)

# Main
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    print("🤖 Bot funcionando...")
    app.run_polling()

if __name__ == "__main__":
    main()
