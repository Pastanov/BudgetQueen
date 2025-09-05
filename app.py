from flask import Flask, request, abort
from twilio.twiml.messaging_response import MessagingResponse
import os, logging, re

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

# ─────────── state & currency helpers ───────────
STATE = {}  # amounts saved internally in ILS
DEFAULT_RATES = {"ILS": 1.0, "USD": 3.7, "EUR": 4.0}
CURRENCY_SYMBOL = {"ILS": "₪", "USD": "$", "EUR": "€"}
ALIASES = {
    "שקל": "ILS", 'ש"ח': "ILS", "שח": "ILS", "₪": "ILS", "ils": "ILS",
    "דולר": "USD", "$": "USD", "usd": "USD", "dollar": "USD",
    "יורו": "EUR", "אירו": "EUR", "eur": "EUR", "euro": "EUR", "€": "EUR",
}

def get_user_state(num: str):
    if num not in STATE:
        STATE[num] = {
            "budget": 0, "remaining": 0, "destination": "",
            "expenses": [], "rates": DEFAULT_RATES.copy(),
            "display_currency": "ILS",
        }
    return STATE[num]

def tw_reply(text: str):
    resp = MessagingResponse()
    resp.message(text)
    return str(resp)

def normalize_currency(word: str):
    return ALIASES.get(word.strip().lower())

def detect_currency_from_text(text: str, default_cur: str):
    t = text
    if "€" in t: return "EUR"
    if "$" in t: return "USD"
    if "₪" in t or 'ש"ח' in t or "שח" in t: return "ILS"
    for k, v in ALIASES.items():
        if k in t.lower():
            return v
    return default_cur

def detect_target_currency(text: str):
    t = text.lower()
    if any(w in t for w in ["בשקלים", "לשקלים", "שקלים", "בשקל"]): return "ILS"
    if any(w in t for w in ["בדולרים", "לדולרים", "בדולר", "לדולר"]): return "USD"
    if any(w in t for w in ["ביורו", "ליורו", "באירו", "לאירו"]): return "EUR"
    if "₪" in t: return "ILS"
    if "$" in t: return "USD"
    if "€" in t: return "EUR"
    return None

def parse_amount(text: str):
    m = re.search(r"(\d[\d,\.]*)", text)
    if not m:
        raise ValueError("no number")
    raw = m.group(1).replace(",", "")
    return int(round(float(raw)))

def to_ils(amount: int, currency: str, rates: dict):
    return int(round(amount * float(rates.get(currency, 1.0))))

def from_ils(amount_ils: int, currency: str, rates: dict):
    return int(round(amount_ils / float(rates.get(currency, 1.0))))

def fmt(amount_ils: int, st):
    cur = st["display_currency"]
    shown = from_ils(amount_ils, cur, st["rates"])
    sym = CURRENCY_SYMBOL.get(cur, "")
    return f"{shown} {sym}" if cur == "ILS" else f"{sym}{shown}"

def fmt_in(amount_ils: int, cur: str, st):
    shown = from_ils(amount_ils, cur, st["rates"])
    sym = CURRENCY_SYMBOL.get(cur, "")
    return f"{shown} {sym}" if cur == "ILS" else f"{sym}{shown}"

# ─────────── routes ───────────
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

    # ─────────── commands ───────────

    # Reset
    if text in ["איפוס", "reset", "start", "התחלה"]:
        STATE[from_number] = {
            "budget": 0, "remaining": 0, "destination": "",
            "expenses": [], "rates": DEFAULT_RATES.copy(), "display_currency": "ILS"
        }
        return tw_reply('🔄 אופסנו הכול. כדי להתחיל: "תקציב 3000" או "יעד: אתונה"\nטיפ: אפשר גם "מטבע: דולר/יורו/שקל"')

    # Display currency
    if text.startswith("מטבע"):
        try:
            word = body_raw.split(":", 1)[1].strip()
            cur = normalize_currency(word) or detect_currency_from_text(word, st["display_currency"])
            if cur not in ["ILS", "USD", "EUR"]: raise ValueError()
            st["display_currency"] = cur
            return tw_reply(f"🎯 מציגים מעכשיו ב־{cur} ({CURRENCY_SYMBOL.get(cur,'')}).\nשערים: USD={st['rates']['USD']} | EUR={st['rates']['EUR']}. לשינוי: \"שער: USD=3.65, EUR=3.95\"")
        except Exception:
            return tw_reply('כתבי כך: מטבע: דולר / יורו / שקל (או USD/EUR/ILS)')

    # Rates
    if text.startswith("שער"):
        try:
            rhs = body_raw.split(":", 1)[1]
            pairs = re.findall(r"(USD|EUR|ILS)\s*=\s*([\d\.]+)", rhs, re.IGNORECASE)
            if not pairs: raise ValueError()
            for cur, rate in pairs:
                st["rates"][cur.upper()] = float(rate)
            return tw_reply(f"עודכן. שערים: USD={st['rates']['USD']} | EUR={st['rates']['EUR']} | ILS=1")
        except Exception:
            return tw_reply('כתבי כך: שער: USD=3.7  או  שער: EUR=4.0  (אפשר גם שניהם בפסיק)')

    # Destination
    if text.startswith("יעד"):
        try:
            dest = body_raw.split(":", 1)[1].strip()
            if not dest: raise ValueError()
            st["destination"] = dest
            return tw_reply(f"מעולה! יעד הוגדר: {dest} ✈️")
        except Exception:
            return tw_reply('כתבי כך: יעד: <שם יעד>\nלדוגמה: יעד: אתונה')

    # Budget (supports "תקציב 3000", "תקציב: $2000", "תקציב 1500€")
    if text.startswith("תקציב"):
        try:
            # מסיר "תקציב", נקודתיים ורווחים – כדי לאפשר גם בלי נקודתיים
            val_part = re.sub(r"^תקציב[:\s]*", "", body_raw, flags=re.IGNORECASE).strip()

            cur = detect_currency_from_text(val_part, st["display_currency"])
            amount = parse_amount(val_part)
            amount_ils = to_ils(amount, cur, st["rates"])

            st["budget"] = amount_ils
            st["remaining"] = amount_ils
            st["expenses"] = []
            st["display_currency"] = cur  # אם צויין מטבע – נציג בו

            src_sym = CURRENCY_SYMBOL.get(cur, "")
            src_txt = f"{src_sym}{amount}" if cur != "ILS" else f"{amount} ₪"
            return tw_reply(f"הוגדר תקציב {fmt(amount_ils, st)} (מקור: {src_txt}). נשאר: {fmt(st['remaining'], st)}.")
        except Exception:
            return tw_reply('אפשר לכתוב גם בלי נקודתיים 🙂\nדוגמאות: תקציב 3000 | תקציב $2000 | תקציב 1500€')

    # Conversion Q: "כמה זה 50$ בשקלים?" / "כמה זה 200 ₪ בדולרים?"
    if "כמה זה" in text:
        try:
            amount = parse_amount(body_raw)
            src_cur = detect_currency_from_text(body_raw, st["display_currency"])
            tgt_cur = detect_target_currency(body_raw) or st["display_currency"]
            amount_ils = to_ils(amount, src_cur, st["rates"])
            converted = fmt_in(amount_ils, tgt_cur, st)
            src_sym = CURRENCY_SYMBOL.get(src_cur, "")
            src_txt = f"{src_sym}{amount}" if src_cur != "ILS" else f"{amount} ₪"
            return tw_reply(f"{src_txt} שווה ~ {converted} לפי השערים הנוכחיים (USD={st['rates']['USD']}, EUR={st['rates']['EUR']}).")
        except Exception:
            return tw_reply('דוגמה: "כמה זה 50$ בשקלים?" | "כמה זה 200 ₪ בדולרים?" | "כמה זה 30€ בשקלים?"')

    # Delete last
    if body_raw == "מחק אחרון":
        if expenses:
            last_amt, last_desc = expenses.pop()
            st["remaining"] += last_amt
            return tw_reply(f"הוצאה אחרונה נמחקה ({fmt(last_amt, st)} – {last_desc}). נשאר: {fmt(st['remaining'], st)}.")
        else:
            return tw_reply("אין הוצאות למחוק.")

    # Delete amount
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
                    return tw_reply(f"הוצאה של {fmt(target_ils, st)} ({desc}) נמחקה. נשאר: {fmt(st['remaining'], st)}.")
            return tw_reply(f"לא נמצאה הוצאה בסך {fmt(target_ils, st)}.")
        except Exception:
            return tw_reply('כתבי כך: מחק 120  |  מחק $10  |  מחק 8€')

    # Update X to Y
    if text.startswith("עדכן"):
        try:
            nums = re.findall(r"(\d[\d,\.]*)", body_raw)
            if len(nums) < 2: raise ValueError()
            old_amt = int(round(float(nums[0].replace(",", ""))))
            new_amt = int(round(float(nums[1].replace(",", ""))))
            cur = detect_currency_from_text(body_raw, st["display_currency"])
            old_ils = to_ils(old_amt, cur, st["rates"])
            new_ils = to_ils(new_amt, cur, st["rates"])
            for i in range(len(expenses) - 1, -1, -1):
                if expenses[i][0] == old_ils:
                    desc = expenses[i][1]
                    expenses[i] = (new_ils, desc)
                    st["remaining"] += (old_ils - new_ils)
                    note = f"\n⚠️ שימי לב: במינוס {fmt(abs(st['remaining']), st)}" if st["remaining"] < 0 else ""
                    return tw_reply(f"הוצאה עודכנה: {fmt(old_ils, st)} → {fmt(new_ils, st)}. נשאר: {fmt(st['remaining'], st)}.{note}")
            return tw_reply(f"לא נמצאה הוצאה של {fmt(old_ils, st)} לעדכן.")
        except Exception:
            return tw_reply('הפורמט: עדכן 50 ל-70  |  עדכן $12 ל-$9  |  עדכן 10€ ל-8€')

    # Report (detailed) — also "סיכום"
    if body_raw in ["הוצאות", "סיכום"]:
        if expenses:
            lines = [f"{i+1}. {fmt(amt, st)} – {desc}" for i, (amt, desc) in enumerate(expenses)]
            total_ils = sum(amt for amt, _ in expenses)
            lines += [
                f"\nסה\"כ הוצאות: {fmt(total_ils, st)}",
                f"יתרה: {fmt(st['remaining'], st)}" + (f"  ⚠️ מינוס {fmt(abs(st['remaining']), st)}" if st["remaining"] < 0 else ""),
            ]
            if st["budget"] > 0:
                lines.append(f"תקציב: {fmt(st['budget'], st)}")
            if st["destination"]:
                lines.append(f"יעד: {st['destination']}")
            lines.append(f"מטבע תצוגה: {st['display_currency']} (USD={st['rates']['USD']} | EUR={st['rates']['EUR']})")
            return tw_reply("\n".join(lines))
        else:
            base = f"יתרה: {fmt(st['remaining'], st)}"
            if st["remaining"] < 0:
                base += f"  ⚠️ מינוס {fmt(abs(st['remaining']), st)}"
            return tw_reply("עדיין לא נרשמו הוצאות.\n" + base)

    # Add expense (allow minus) — תומך גם ב"הוצאה ..." וגם בלי
    if any(ch.isdigit() for ch in body_raw):
        if st["budget"] == 0:
            return tw_reply('קודם צריך להגדיר תקציב 📝\nכתבי: תקציב 3000 או תקציב $2000')
        try:
            # מסיר "הוצאה" בתחילת הטקסט אם הופיעה (עם/בלי נקודתיים/רווח)
            cleaned = re.sub(r"^הוצאה[:\s]*", "", body_raw, flags=re.IGNORECASE).strip()

            cur = detect_currency_from_text(cleaned, st["display_currency"])
            amount = parse_amount(cleaned)
            amount_ils = to_ils(amount, cur, st["rates"])

            # description
            if "–" in cleaned:
                description = cleaned.split("–", 1)[1].strip()
            elif "-" in cleaned:
                description = cleaned.split("-", 1)[1].strip()
            else:
                description = "הוצאה"

            expenses.append((amount_ils, description))
            st["remaining"] -= amount_ils

            note = f"\n⚠️ שימי לב: במינוס {fmt(abs(st['remaining']), st)}" if st["remaining"] < 0 else ""
            return tw_reply(f"נוספה הוצאה: {fmt(amount_ils, st)} – {description}\nנשאר: {fmt(st['remaining'], st)}.{note}")
        except Exception:
            return tw_reply("לא הצלחתי לזהות את הסכום, נסי שוב 🙂")

    # Help
    return tw_reply('כדי להתחיל: "תקציב 3000" או "יעד: אתונה"\n'
                    'פקודות: "מטבע: דולר/יורו/שקל", "שער: USD=3.65", '
                    '"כמה זה 50$ בשקלים?", "הוצאות"/"סיכום", '
                    '"מחק אחרון", "מחק 120", "עדכן 50 ל-70", "איפוס"')

if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
