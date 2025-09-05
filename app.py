from flask import Flask, request, abort
from twilio.twiml.messaging_response import MessagingResponse
import os

app = Flask(__name__)

# סטייט לפי מספר משתמש
STATE = {}  # { "whatsapp:+9725...": {"budget":0,"remaining":0,"destination":"","expenses":[(amt,desc)]} }

def get_user_state(from_number: str):
    if from_number not in STATE:
        STATE[from_number] = {
            "budget": 0,
            "remaining": 0,
            "destination": "",
            "expenses": []
        }
    return STATE[from_number]

@app.route("/", methods=["GET"])
def home():
    return "Budget Queen WhatsApp Bot - OK", 200

@app.route("/whatsapp", methods=["GET", "POST"])
def whatsapp_bot():
    if request.method == "GET":
        return "Webhook is ready", 200

    from_number = request.form.get("From", "")
    incoming_msg = (request.form.get("Body") or "").strip()

    if not from_number:
        abort(400)

    st = get_user_state(from_number)
    expenses = st["expenses"]

    text = incoming_msg.lower()

    # יעד: פריז
    if text.startswith("יעד"):
        try:
            destination = incoming_msg.split(":", 1)[1].strip()
            st["destination"] = destination
            return str(_reply(f"מעולה! יעד הוגדר: {destination}"))
        except:
            return str(_reply("כתבי כך: יעד: <שם יעד>"))

    # תקציב: 3000
    if text.startswith("תקציב"):
        try:
            val = incoming_msg.split(":", 1)[1]
            val = val.replace('ש"ח', "").replace("₪", "").strip()
            budget = int(val)
            st["budget"] = budget
            st["remaining"] = budget
            st["expenses"] = []
            return str(_reply(f"הוגדר תקציב {budget} ₪. נשאר: {budget} ₪."))
        except:
            return str(_reply("כתבי כך: תקציב: <סכום>"))

    # מחק אחרון
    if incoming_msg == "מחק אחרון":
        if expenses:
            last_amt, last_desc = expenses.pop()
            st["remaining"] += last_amt
            return str(_reply(f"הוצאה אחרונה נמחקה ({last_amt} ₪ – {last_desc}). נשאר {st['remaining']} ₪."))
        else:
            return str(_reply("אין הוצאות למחוק."))

    # מחק 120
    if text.startswith("מחק "):
        try:
            amount = int(incoming_msg.split()[1])
            for i in range(len(expenses) - 1, -1, -1):
                if expenses[i][0] == amount:
                    desc = expenses[i][1]
                    expenses.pop(i)
                    st["remaining"] += amount
                    return str(_reply(f"הוצאה של {amount} ₪ ({desc}) נמחקה. נשאר {st['remaining']} ₪."))
            return str(_reply(f"לא נמצאה הוצאה בסך {amount} ₪."))
        except:
            return str(_reply("כתבי כך: מחק <סכום>"))

    # עדכן 50 ל-70
    if text.startswith("עדכן"):
        try:
            parts = incoming_msg.replace("-", " ").split()
            old_amount = int(parts[1])
            new_amount = int(parts[3])
            for i in range(len(expenses) - 1, -1, -1):
                if expenses[i][0] == old_amount:
                    desc = expenses[i][1]
                    expenses[i] = (new_amount, desc)
                    st["remaining"] += (old_amount - new_amount)
                    return str(_reply(f"הוצאה עודכנה: {old_amount} → {new_amount}. נשאר {st['remaining']} ₪."))
            return str(_reply(f"לא נמצאה הוצאה של {old_amount} ₪ לעדכן."))
        except:
            return str(_reply("הפורמט: עדכן <סכום ישן> ל-<סכום חדש>"))

    # הוצאות
    if incoming_msg == "הוצאות":
        if expenses:
            lines = [f"{i+1}. {amt} ₪ – {desc}" for i, (amt, desc) in enumerate(expenses)]
            total = sum(amt for amt, _ in expenses)
            lines.append(f"\nסה\"כ הוצאות: {total} ₪")
            lines.append(f"נשאר: {st['remaining']} ₪")
            if st["destination"]:
                lines.append(f"יעד: {st['destination']}")
            return str(_reply("\n".join(lines)))
        else:
            return str(_reply("עדיין לא נרשמו הוצאות."))

    # הוספת הוצאה – כל הודעה עם מספר (למשל: "120 – קפה")
    if any(ch.isdigit() for ch in incoming_msg):
        try:
            digits = "".join(ch for ch in incoming_msg if ch.isdigit())
            amount = int(digits)
            if "–" in incoming_msg:
                description = incoming_msg.split("–", 1)[1].strip()
            elif "-" in incoming_msg:
                description = incoming_msg.split("-", 1)[1].strip()
            else:
                description = "הוצאה"
            expenses.append((amount, description))
            st["remaining"] -= amount
            return str(_reply(f"נוספה הוצאה: {amount} ₪ – {description}\nנשאר: {st['remaining']} ₪."))
        except:
            return str(_reply("לא הצלחתי לזהות את הסכום, נסי שוב 🙂"))

    return str(_reply("כדי להתחיל כתבי: יעד: ___ או תקציב: ___\nלדוגמה: תקציב: 3000"))

def _reply(text: str):
    resp = MessagingResponse()
    resp.message(text)
    return resp

if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
