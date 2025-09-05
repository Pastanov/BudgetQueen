from flask import Flask, request, abort
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

# סטייט לפי מספר טלפון (כדי שכל משתמש/ת יקבל/תקבל תקציב נפרד)
# מבנה: { "whatsapp:+9725XXXXXXX": {"budget":0,"remaining":0,"destination":"","expenses":[(amt,desc)]} }
STATE = {}

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

# אפשר להשאיר /whatsapp אם בא לך; רק תזכרי לשים אותו בטוויליו
@app.route("/whatsapp", methods=["GET", "POST"])
def whatsapp_bot():
    if request.method == "GET":
        # כדי שלא תראי NOT FOUND כשנכנסים בדפדפן
        return "Webhook is ready", 200

    from_number = request.form.get("From", "")
    incoming_msg = (request.form.get("Body") or "").strip()

    if not from_number:
        abort(400)

    st = get_user_state(from_number)
    budget = st["budget"]
    remaining = st["remaining"]
    destination = st["destination"]
    expenses = st["expenses"]

    resp = MessagingResponse()
    msg = resp.message()

    text = incoming_msg.lower()

    # הגדרת יעד — "יעד: פריז"
    if text.startswith("יעד"):
        try:
            destination = incoming_msg.split(":", 1)[1].strip()
            st["destination"] = destination
            msg.body(f"מעולה! יעד הוגדר: {destination}")
        except:
            msg.body("כתבי כך: יעד: <שם יעד>")
        return str(resp)

    # הגדרת תקציב — "תקציב: 3000" או "תקציב: 3000 ש\"ח"
    if text.startswith("תקציב"):
        try:
            val = incoming_msg.split(":", 1)[1]
            val = val.replace('ש"ח', "").replace("₪", "").strip()
            budget = int(val)
            st["budget"] = budget
            st["remaining"] = budget
            st["expenses"] = []
            msg.body(f"הוגדר תקציב {budget} ₪. נשאר: {budget} ₪.")
        except:
            msg.body("כתבי כך: תקציב: <סכום>")
        return str(resp)

    # מחיקת הוצאה אחרונה — "מחק אחרון"
    if incoming_msg == "מחק אחרון":
        if expenses:
            last_amt, last_desc = expenses.pop()
            st["remaining"] += last_amt
            msg.body(f"הוצאה אחרונה נמחקה ({last_amt} ₪ – {last_desc}). נשאר {st['remaining']} ₪.")
        else:
            msg.body("אין הוצאות למחוק.")
        return str(resp)

    # מחיקת הוצאה לפי סכום — "מחק 120"
    if text.startswith("מחק "):
        try:
            amount = int(incoming_msg.split()[1])
            for i in range(len(expenses) - 1, -1, -1):
                if expenses[i][0] == amount:
                    desc = expenses[i][1]
                    expenses.pop(i)
                    st["remaining"] += amount
                    msg.body(f"הוצאה של {amount} ₪ ({desc}) נמחקה. נשאר {st['remaining']} ₪.")
                    break
            else:
                msg.body(f"לא נמצאה הוצאה בסך {amount} ₪.")
        except:
            msg.body("כתבי כך: מחק <סכום>")
        return str(resp)

    # עדכון הוצאה — "עדכן 50 ל-70"
    if text.startswith("עדכן"):
        try:
            parts = incoming_msg.replace("-", " ").split()
            # צפוי מבנה: ["עדכן", "50", "ל", "70"]
            old_amount = int(parts[1])
            new_amount = int(parts[3])
            for i in range(len(expenses) - 1, -1, -1):
                if expenses[i][0] == old_amount:
                    desc = expenses[i][1]
                    expenses[i] = (new_amount, desc)
                    st["remaining"] += (old_amount - new_amount)
                    msg.body(f"הוצאה עודכנה: {old_amount} → {new_amount}. נשאר {st['remaining']} ₪.")
                    break
            else:
                msg.body(f"לא נמצאה הוצאה של {old_amount} ₪ לעדכן.")
        except:
            msg.body("הפורמט: עדכן <סכום ישן> ל-<סכום חדש>")
        return str(resp)

    # הצגת הוצאות — "הוצאות"
    if incoming_msg == "הוצאות":
        if expenses:
            lines = [f"{i+1}. {amt} ₪ – {desc}" for i, (amt, desc) in enumerate(expenses)]
            total = sum(amt for amt, _ in expenses)
            lines.append(f"\nסה\"כ הוצאות: {total} ₪")
            lines.append(f"נשאר: {st['remaining']} ₪")
            if destination:
                lines.append(f"יעד: {destination}")
            msg.body("\n".join(lines))
        else:
            msg.body("עדיין לא נרשמו הוצאות.")
        return str(resp)

    # הוספת הוצאה — כל הודעה שיש בה מספר (למשל: "120 – קפה וסנדוויץ'")
    if any(ch.isdigit() for ch in incoming_msg):
        try:
            # חילוץ סכום ראשון שמופיע
            digits = "".join(ch for ch in incoming_msg if ch.isdigit())
            amount = int(digits)
            # תיאור אחרי '–' (מקף ארוך) או '-' (מקף רגיל)
            if "–" in incoming_msg:
                description = incoming_msg.split("–", 1)[1].strip()
            elif "-" in incoming_msg:
                description = incoming_msg.split("-", 1)[1].strip()
            else:
                description = "הוצאה"
            expenses.append((amount, description))
            st["remaining"] -= amount
            msg.body(f"נוספה הוצאה: {amount} ₪ – {description}\nנשאר: {st['remaining']} ₪.")
        except:
            msg.body("לא הצלחתי לזהות את הסכום, נסי שוב 🙂")
        return str(resp)

    # ברירת מחדל
    msg.body("כדי להתחיל כתבי: יעד: ___ או תקציב: ___\nלדוגמה: תקציב: 3000")
    return str(resp)

if __name__ == "__main__":
    # חשוב כדי שיעבוד ב-Render
    import os
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
from flask import Flask, request, abort
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

# סטייט לפי מספר טלפון (כדי שכל משתמש/ת יקבל/תקבל תקציב נפרד)
# מבנה: { "whatsapp:+9725XXXXXXX": {"budget":0,"remaining":0,"destination":"","expenses":[(amt,desc)]} }
STATE = {}

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

# אפשר להשאיר /whatsapp אם בא לך; רק תזכרי לשים אותו בטוויליו
@app.route("/whatsapp", methods=["GET", "POST"])
def whatsapp_bot():
    if request.method == "GET":
        # כדי שלא תראי NOT FOUND כשנכנסים בדפדפן
        return "Webhook is ready", 200

    from_number = request.form.get("From", "")
    incoming_msg = (request.form.get("Body") or "").strip()

    if not from_number:
        abort(400)

    st = get_user_state(from_number)
    budget = st["budget"]
    remaining = st["remaining"]
    destination = st["destination"]
    expenses = st["expenses"]

    resp = MessagingResponse()
    msg = resp.message()

    text = incoming_msg.lower()

    # הגדרת יעד — "יעד: פריז"
    if text.startswith("יעד"):
        try:
            destination = incoming_msg.split(":", 1)[1].strip()
            st["destination"] = destination
            msg.body(f"מעולה! יעד הוגדר: {destination}")
        except:
            msg.body("כתבי כך: יעד: <שם יעד>")
        return str(resp)

    # הגדרת תקציב — "תקציב: 3000" או "תקציב: 3000 ש\"ח"
    if text.startswith("תקציב"):
        try:
            val = incoming_msg.split(":", 1)[1]
            val = val.replace('ש"ח', "").replace("₪", "").strip()
            budget = int(val)
            st["budget"] = budget
            st["remaining"] = budget
            st["expenses"] = []
            msg.body(f"הוגדר תקציב {budget} ₪. נשאר: {budget} ₪.")
        except:
            msg.body("כתבי כך: תקציב: <סכום>")
        return str(resp)

    # מחיקת הוצאה אחרונה — "מחק אחרון"
    if incoming_msg == "מחק אחרון":
        if expenses:
            last_amt, last_desc = expenses.pop()
            st["remaining"] += last_amt
            msg.body(f"הוצאה אחרונה נמחקה ({last_amt} ₪ – {last_desc}). נשאר {st['remaining']} ₪.")
        else:
            msg.body("אין הוצאות למחוק.")
        return str(resp)

    # מחיקת הוצאה לפי סכום — "מחק 120"
    if text.startswith("מחק "):
        try:
            amount = int(incoming_msg.split()[1])
            for i in range(len(expenses) - 1, -1, -1):
                if expenses[i][0] == amount:
                    desc = expenses[i][1]
                    expenses.pop(i)
                    st["remaining"] += amount
                    msg.body(f"הוצאה של {amount} ₪ ({desc}) נמחקה. נשאר {st['remaining']} ₪.")
                    break
            else:
                msg.body(f"לא נמצאה הוצאה בסך {amount} ₪.")
        except:
            msg.body("כתבי כך: מחק <סכום>")
        return str(resp)

    # עדכון הוצאה — "עדכן 50 ל-70"
    if text.startswith("עדכן"):
        try:
            parts = incoming_msg.replace("-", " ").split()
            # צפוי מבנה: ["עדכן", "50", "ל", "70"]
            old_amount = int(parts[1])
            new_amount = int(parts[3])
            for i in range(len(expenses) - 1, -1, -1):
                if expenses[i][0] == old_amount:
                    desc = expenses[i][1]
                    expenses[i] = (new_amount, desc)
                    st["remaining"] += (old_amount - new_amount)
                    msg.body(f"הוצאה עודכנה: {old_amount} → {new_amount}. נשאר {st['remaining']} ₪.")
                    break
            else:
                msg.body(f"לא נמצאה הוצאה של {old_amount} ₪ לעדכן.")
        except:
            msg.body("הפורמט: עדכן <סכום ישן> ל-<סכום חדש>")
        return str(resp)

    # הצגת הוצאות — "הוצאות"
    if incoming_msg == "הוצאות":
        if expenses:
            lines = [f"{i+1}. {amt} ₪ – {desc}" for i, (amt, desc) in enumerate(expenses)]
            total = sum(amt for amt, _ in expenses)
            lines.append(f"\nסה\"כ הוצאות: {total} ₪")
            lines.append(f"נשאר: {st['remaining']} ₪")
            if destination:
                lines.append(f"יעד: {destination}")
            msg.body("\n".join(lines))
        else:
            msg.body("עדיין לא נרשמו הוצאות.")
        return str(resp)

    # הוספת הוצאה — כל הודעה שיש בה מספר (למשל: "120 – קפה וסנדוויץ'")
    if any(ch.isdigit() for ch in incoming_msg):
        try:
            # חילוץ סכום ראשון שמופיע
            digits = "".join(ch for ch in incoming_msg if ch.isdigit())
            amount = int(digits)
            # תיאור אחרי '–' (מקף ארוך) או '-' (מקף רגיל)
            if "–" in incoming_msg:
                description = incoming_msg.split("–", 1)[1].strip()
            elif "-" in incoming_msg:
                description = incoming_msg.split("-", 1)[1].strip()
            else:
                description = "הוצאה"
            expenses.append((amount, description))
            st["remaining"] -= amount
            msg.body(f"נוספה הוצאה: {amount} ₪ – {description}\nנשאר: {st['remaining']} ₪.")
        except:
            msg.body("לא הצלחתי לזהות את הסכום, נסי שוב 🙂")
        return str(resp)

    # ברירת מחדל
    msg.body("כדי להתחיל כתבי: יעד: ___ או תקציב: ___\nלדוגמה: תקציב: 3000")
    return str(resp)

if __name__ == "__main__":
    # חשוב כדי שיעבוד ב-Render
    import os
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
ש
