from flask import Flask, request, abort
from twilio.twiml.messaging_response import MessagingResponse
import os
import logging
import re

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

# סטייט פר-משתמש
# amounts נשמרים תמיד ב-ILS (שקלים). מציגים לפי display_currency.
STATE = {}  # { "whatsapp:+9725...": {"budget":int,"remaining":int,"destination":str,"expenses":[(ils_amt,desc)],"rates":{...},"display_currency":"ILS"} }

DEFAULT_RATES = {"ILS": 1.0, "USD": 3.7, "EUR": 4.0}
CURRENCY_SYMBOL = {"ILS": "₪", "USD": "$", "EUR": "€"}

ALIASES = {
    "שקל": "ILS", 'ש"ח': "ILS", "שח": "ILS", "₪": "ILS", "ils": "ILS",
    "דולר": "USD", "$": "USD", "usd": "USD", "dollar": "USD",
    "יורו": "EUR", "אירו": "EUR", "eur": "EUR", "euro": "EUR", "€": "EUR",
}

def get_user_state(from_number: str):
    if from_number not in STATE:
        STATE[from_number] = {
            "budget": 0,           # ב-ILS
            "remaining": 0,        # ב-ILS
            "destination": "",
            "expenses": [],        # [(amount_ils, desc)]
            "rates": DEFAULT_RATES.copy(),
            "display_currency": "ILS",  # איך להציג למשתמש
        }
    return STATE[from_number]

def reply(text: str):
    resp = MessagingResponse()
    resp.message(text)
    return str(resp)

def normalize_currency(word: str):
    word = word.strip().lower()
    return ALIASES.get(word, None)

def detect_currency_from_text(text: str, default_cur: str):
    t = text
    # לפי סימן
    if "€" in t: return "EUR"
    if "$" in t: return "USD"
    if "₪" in t or 'ש"ח' in t or "שח" in t: return "ILS"
    # לפי מילים
    for k, v in ALIASES.items():
        if k in t.lower():
            return v
    return default_cur

def parse_amount(text: str):
    # מחלץ את המספר הראשון (גם אם יש פסיקים/נקודה)
    m = re.search(r"(\d[\d,\.]*)", text)
    if not m:
        raise ValueError("no number")
    raw = m.group(1).replace(",", "")
    # מתירים מספרים שלמים (נמיר ל-int)
    val = float(raw)
    return int(round(val))

def to_ils(amount: int, currency: str, rates: dict):
    r = rates.get(currency, 1.0)
    return int(round(amount * r))

def from_ils(amount_ils: int, currency: str, rates: dict):
    r = rates.get(currency, 1.0)
    return int(round(amount_ils / r))

def fmt(amount_ils: int, st):
    cur = st["display_currency"]
    shown = from_ils(amount_ils, cur, st["rates"])
    sym = CURRENCY_SYMBOL.get(cur, "")
    # אם ILS, נשים את הסימן אחרי מספר בעברית
    if cur == "ILS":
        return f"{shown} {sym}".strip()
    return f"{sym}{shown}".strip()

@app.route("/", methods=["GET"])
def home():
    return "Budget Queen WhatsApp Bot - OK", 200

@app.route("/whatsapp", methods=["GET", "POST"])
def whatsapp():
    if request.method == "GET":
        return "Webhook is ready", 200

    from_number = request.form.get("From", "")
    body_raw = (request.form.get("Body") or "").strip()
    text = body_raw.lower()
    if not from_number:
        abort(400)

    st = get_user_state(from_number)
    expenses = st["expenses"]

    logging.info("Incoming | From=%s | Body=%r", from_number, body_raw)

    # ===== פקודות =====

    # איפוס
    if text in ["איפוס", "reset", "start", "התחלה"]:
        STATE[from_number] = {
            "budget": 0, "remaining": 0, "destination": "",
            "expenses": [], "rates": DEFAULT_RATES.copy(), "display_currency": "ILS"
        }
        return reply('🔄 אופסנו הכול. כתבי: "תקציב: 3000" או "יעד: אתונה"\nטיפ: אפשר גם "מטבע: דולר/יורו/שקל"')

    # מטבע: ... (בחירת מטבע תצוגה/קלט ברירת מחדל)
    # דוגמאות: מטבע: דולר | מטבע: USD | מטבע: €
    if text.startswith("מטבע"):
        try:
            word = body_raw.split(":", 1)[1].strip()
            cur = normalize_currency(word) or detect_currency_from_text(word, st["display_currency"])
            if cur not in ["ILS", "USD", "EUR"]:
                raise ValueError()
            st["display_currency"] = cur
            return reply(f"🎯 מעכשיו מציגים ב־{cur} ({CURRENCY_SYMBOL.get(cur,'')}).\nשערים נוכחיים: USD={st['rates']['USD']} | EUR={st['rates']['EUR']}. אפשר לשנות עם: שער: USD=3.65")
        except Exception:
            return reply('כתבי כך: מטבע: דולר / יורו / שקל (או USD/EUR/ILS)')

    # שער: USD=3.7  או  שער: EUR=4.0
    if text.startswith("שער"):
        try:
            rhs = body_raw.split(":", 1)[1].strip()
            # תומך בכמה עדכונים: "שער: USD=3.6, EUR=4"
            pairs = re.findall(r"(USD|EUR|ILS)\s*=\s*([\d\.]+)", rhs, re.IGNORECASE)
            if not pairs:
                raise ValueError()
            for cur, rate in pairs:
                st["rates"][cur.upper()] = float(rate)
            return reply(f"עודכן. שערים: USD={st['rates']['USD']} | EUR={st['rates']['EUR']} | ILS=1")
        except Exception:
            return reply('כתבי כך: שער: USD=3.7  או  שער: EUR=4.0  (אפשר גם שניהם עם פסיק)')

    # יעד: ...
    if text.startswith("יעד"):
        try:
            dest = body_raw.split(":", 1)[1].strip()
            if not dest:
                raise ValueError("empty")
            st["destination"] = dest
            return reply(f"מעולה! יעד הוגדר: {dest} ✈️")
        except Exception:
            return reply('כתבי כך: יעד: <שם יעד>\nלדוגמה: יעד: אתונה')

    # תקציב: 3000 (אפשר במטבע כלשהו, לדוגמה: תקציב: $2000, תקציב: 1500€)
    if text.startswith("תקציב"):
        try:
            val_part = body_raw.split(":", 1)[1].strip()
            cur = detect_currency_from_text(val_part, st["display_currency"])
            amount = parse_amount(val_part)
            amount_ils = to_ils(amount, cur, st["rates"])
            st["budget"] = amount_ils
            st["remaining"] = amount_ils
            st["expenses"] = []
            return reply(f"הוגדר תקציב {fmt(amount_ils, st)}. נשאר: {fmt(st['remaining'], st)}.")
        except Exception:
            return reply('כתבי כך: תקציב: <סכום>\nדוגמאות: תקציב: 3000 | תקציב: $2000 | תקציב: 1500€')

    # מחק אחרון
    if body_raw == "מחק אחרון":
        if expenses:
            last_amt_ils, last_desc = expenses.pop()
            st["remaining"] += last_amt_ils
            return reply(f"הוצאה אחרונה נמחקה ({fmt(last_amt_ils, st)} – {last_desc}). נשאר: {fmt(st['remaining'], st)}.")
        else:
            return reply("אין הוצאות למחוק.")

    # מחק <סכום>  (מחפש לפי סכום במטבע התצוגה/סימן בהודעה)
    if text.startswith("מחק "):
        try:
            cur = detect_currency_from_text(body_raw, st["display_currency"])
            amount = parse_amount(body_raw)
            target_ils = to_ils(amount, cur, st["rates"])
            for i in range(len(expenses) - 1, -1, -1):
                if expenses[i][0] == target_ils:
                    desc = expenses[i][1]
                    expenses.pop(i)
                    st["remaining"] += target_ils
                    return reply(f"הוצאה של {fmt(target_ils, st)} ({desc}) נמחקה. נשאר: {fmt(st['remaining'], st)}.")
            return reply(f"לא נמצאה הוצאה בסך {fmt(target_ils, st)}.")
        except Exception:
            return reply('כתבי כך: מחק 120  |  מחק $10  |  מחק 8€')

    # עדכן X ל-Y  (תומך בסימנים/מטבעות שונים)
    if text.startswith("עדכן"):
        try:
            # נשלוף שני סכומים (הישן והחדש)
            nums = re.findall(r"(\d[\d,\.]*)", body_raw)
            if len(nums) < 2:
                raise ValueError()
            old_amt = int(round(float(nums[0].replace(",", ""))))
            new_amt = int(round(float(nums[1].replace(",", ""))))
            old_cur = detect_currency_from_text(body_raw, st["display_currency"])
            new_cur = old_cur  # אם אין ציון נפרד, נניח אותו מטבע
            old_ils = to_ils(old_amt, old_cur, st["rates"])
            new_ils = to_ils(new_amt, new_cur, st["rates"])
            for i in range(len(expenses) - 1, -1, -1):
                if expenses[i][0] == old_ils:
                    desc = expenses[i][1]
                    expenses[i] = (new_ils, desc)
                    st["remaining"] += (old_ils - new_ils)
                    return reply(f"הוצאה עודכנה: {fmt(old_ils, st)} → {fmt(new_ils, st)}. נשאר: {fmt(st['remaining'], st)}.")
            return reply(f"לא נמצאה הוצאה של {fmt(old_ils, st)} לעדכן.")
        except Exception:
            return reply('הפורמט: עדכן 50 ל-70  |  עדכן $12 ל-$9  |  עדכן 10€ ל-8€')

    # הוצאות
    if body_raw == "הוצאות":
        if expenses:
            lines = [f"{i+1}. {fmt(amt, st)} – {desc}" for i, (amt, desc) in enumerate(expenses)]
            total_ils = sum(amt for amt, _ in expenses)
            lines.append(f"\nסה\"כ הוצאות: {fmt(total_ils, st)}")
            lines.append(f"נשאר: {fmt(st['remaining'], st)}")
            if st["destination"]:
                lines.append(f"יעד: {st['destination']}")
            lines.append(f"מטבע תצוגה: {st['display_currency']} (USD={st['rates']['USD']} | EUR={st['rates']['EUR']})")
            return reply("\n".join(lines))
        else:
            return reply("עדיין לא נרשמו הוצאות.")

    # הוספת הוצאה — כל הודעה עם מספר (תומך בסימנים/מטבע)
    if any(ch.isdigit() for ch in body_raw):
        if st["budget"] == 0:
            return reply('קודם צריך להגדיר תקציב 📝\nכתבי: תקציב: 3000 או תקציב: $2000')
        try:
            cur = detect_currency_from_text(body_raw, st["display_currency"])
            amount = parse_amount(body_raw)
            amount_ils = to_ils(amount, cur, st["rates"])

            if amount_ils > st["remaining"]:
                return reply(f"הסכום {fmt(amount_ils, st)} גדול מהיתרה ({fmt(st['remaining'], st)}). נסי סכום קטן יותר או עדכני תקציב.")

            # תיאור אחרי '–' או '-'
            if "–" in body_raw:
                description = body_raw.split("–", 1)[1].strip()
            elif "-" in body_raw:
                description = body_raw.split("-", 1)[1].strip()
            else:
                description = "הוצאה"

            expenses.append((amount_ils, description))
            st["remaining"] -= amount_ils
            return reply(f"נוספה הוצאה: {fmt(amount_ils, st)} – {description}\nנשאר: {fmt(st['remaining'], st)}.")
        except Exception:
            return reply("לא הצלחתי לזהות את הסכום, נסי שוב 🙂")

    # ברירת מחדל
    return reply('כדי להתחיל: "תקציב: 3000" או "יעד: אתונה"\nפקודות: "מטבע: דולר/יורו/שקל", "שער: USD=3.65", "הוצאות", "מחק אחרון", "מחק 120", "עדכן 50 ל-70", "איפוס"')

if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
