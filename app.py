# app.py
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import requests

app = Flask(__name__)

# הכתובת של ה-App Script שלך (להדביק פה!)
GOOGLE_SHEET_URL = "https://script.google.com/macros/s/AKfycbyuGcPyJ97ZXY7AUcrIt9DwRQIGxn6isvvEBzzqMb4BDeFB76YIuX0l1XgmLL4GrT1Z/exec"

# תגובות חמודות לקטגוריות
fun_responses = {
    "אוכל": "יואו מקווה שהיה טעים 😋",
    "קניות": "וואייי תתחדשי 👗",
    "בילויים": "איזה כיף! מגיע לך 🎉",
}

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    incoming_msg = request.values.get("Body", "").strip()
    sender = request.values.get("From", "אנונימי")

    resp = MessagingResponse()
    msg = resp.message()

    if incoming_msg.startswith("הוצאה"):
        # דוגמה: "הוצאה 50 אוכל פיצה"
        parts = incoming_msg.split(" ", 3)
        if len(parts) >= 3:
            try:
                amount = int(parts[1])
                category = parts[2]
                what = parts[3] if len(parts) > 3 else ""

                # שליחה ל-Google Sheets
                data = {"name": sender, "category": category, "amount": amount}
                requests.post(GOOGLE_SHEET_URL, json=data)

                # תגובה חמודה
                fun_msg = fun_responses.get(category, "💸 אחלה בחירה!")
                msg.body(f"✅ נוספה הוצאה: {amount}₪ – {category} ({what})\n{fun_msg}")
            except:
                msg.body("❌ סכום לא תקין. דוגמה: הוצאה 50 אוכל פיצה")
        else:
            msg.body("כדי להוסיף הוצאה, כתבי: הוצאה [סכום] [קטגוריה] [על מה]")
    elif incoming_msg == "סיכום":
        msg.body("📊 הסיכום שלך נמצא בגוגל שיטס שלך ✅")
    else:
        msg.body("❓ לא הבנתי... נסי למשל: 'הוצאה 50 אוכל פיצה' או 'סיכום'")

    return str(resp)

if __name__ == "__main__":
    app.run(port=5000, debug=True)
