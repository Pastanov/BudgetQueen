from flask import Flask, request, abort
from twilio.twiml.messaging_response import MessagingResponse
import os, logging, re, json, random, string

# ===== Logging =====
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("budget-queen")

# ===== Flask =====
app = Flask(__name__)

# ===== Redis (persistent state) =====
USE_REDIS = False
r = None
try:
    from redis import Redis
    REDIS_URL = os.getenv("REDIS_URL")
    if REDIS_URL:
        r = Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        try:
            r.ping()
            USE_REDIS = True
            log.info("Redis enabled ✅")
        except Exception as e:
            r = None
            USE_REDIS = False
            log.warning("Redis ping failed; falling back to in-memory ⚠️ %s", e)
    else:
        log.warning("REDIS_URL not set; falling back to in-memory state ⚠️")
except Exception as e:
    log.warning("Redis import failed; using in-memory state ⚠️ %s", e)

# ===== Constants =====
DEFAULT_RATES = {"ILS": 1.0, "USD": 3.7, "EUR": 4.0}
CURRENCY_SYMBOL = {"ILS": "₪", "USD": "$", "EUR": "€"}

ALIASES = {
    "שקל": "ILS", 'ש"ח': "ILS", "שח": "ILS", "₪": "ILS", "ils": "ILS",
    "דולר": "USD", "$": "USD", "usd": "USD", "dollar": "USD",
    "יורו": "EUR", "אירו": "EUR", "eur": "EUR", "euro": "EUR", "€": "EUR",
}

# מילות מפתח -> קטגוריה
CATEGORY_MAP = {
    # אוכל/שתייה
    "קפה": "אוכל", "פיצה": "אוכל", "מסעדה": "אוכל", "אוכל": "אוכל",
    "שתיה": "אוכל", "משקה": "אוכל", "גלידה": "אוכל", "סופר": "אוכל",
    "מכולת": "אוכל", "מאפה": "אוכל", "בירה": "אוכל", "יין": "אוכל",
    # תחבורה
    "מונית": "תחבורה", "טקסי": "תחבורה", "אובר": "תחבורה", "uber": "תחבורה",
    "בולט": "תחבורה", "bolt": "תחבורה", "רכבת": "תחבורה", "אוטובוס": "תחבורה",
    "דלק": "תחבורה", "חנייה": "תחבורה",
    # קניות
    "שמלה": "קניות", "חולצה": "קניות", "בגד": "קניות", "בגדים": "קניות",
    "סנדלים": "קניות", "נעל": "קניות", "נעליים": "קניות", "קניות": "קניות",
    "מתנה": "קניות", "גאדג'ט": "קניות", "שופינג": "קניות",
    # לינה
    "מלון": "לינה", "הוסטל": "לינה", "airbnb": "לינה", "דירה": "לינה", "לינה": "לינה",
    # אטרקציות
    "מוזיאון": "אטרקציות", "פארק": "אטרקציות", "כניסה": "אטרקציות", "סיור": "אטרקציות",
    "שייט": "אטרקציות", "אטרקציה": "אטרקציות",
    # בריאות/ביטוח
    "ביטוח": "בריאות", "תרופה": "בריאות", "רופא": "בריאות", "בדיקה": "בריאות",
    # תקשורת
    "סים": "תקשורת", "טלפון": "תקשורת", "חבילת גלישה": "תקשורת", "wifi": "תקשורת",
}

# ===== In-memory fallback =====
MEM_TRIPS = {}   # key -> trip state
MEM_USERS = {}   # phone -> user meta

# ===== State models =====
def default_state():
    return {
        "budget": 0,
        "remaining": 0,
        "destination": "",
        "expenses": [],  # list of dicts: {amt_ils:int, desc:str, cat:str, added_by:str}
        "rates": DEFAULT_RATES.copy(),
        "display_currency": "ILS",
        "members": [],
        "code": "",
    }

def tw_reply(text: str):
    resp = MessagingResponse()
    resp.message(text)
    return str(resp)

# ===== Trip/User keys =====
def trip_key(code): return f"trip:{code}"
def user_key(num): return f"user:{num}"

def random_code(n=6):
    return ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(n))

# ===== Trip/User persistence =====
def trip_exists(code):
    key = trip_key(code)
    if USE_REDIS:
        try:
            return bool(r.exists(key))
        except:
            return False
    return key in MEM_TRIPS

def load_trip(code):
    key = trip_key(code)
    if USE_REDIS:
        try:
            raw = r.get(key)
            if raw:
                return json.loads(raw)
            return None
        except:
            return None
    return MEM_TRIPS.get(key)

def save_trip(code, st):
    key = trip_key(code)
    if USE_REDIS:
        try:
            r.set(key, json.dumps(st))
            return
        except Exception as e:
            log.warning("Redis write failed for trip %s: %s", code, e)
    MEM_TRIPS[key] = st

def load_user(num):
    key = user_key(num)
    if USE_REDIS:
        try:
            raw = r.get(key)
            if raw:
                return json.loads(raw)
        except:
            pass
    return MEM_USERS.get(key, {"active_trip": f"SELF:{num}"})

def save_user(num, meta):
    key = user_key(num)
    if USE_REDIS:
        try:
            r.set(key, json.dumps(meta)); return
        except:
            pass
    MEM_USERS[key] = meta

def ensure_self_trip(num):
    """יוצר/טוען טיול אישי SELF:<num> ומוסיף את המשתמש כחבר"""
    code = f"SELF:{num}"
    st = load_trip(code)
    if st is None:
        st = default_state()
        st["code"] = code
        st["members"] = [num]
        save_trip(code, st)
    else:
        if num not in st.get("members", []):
            st["members"].append(num)
            save_trip(code, st)
    return code, st

# ===== Currency helpers =====
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

def fmt_in(amount_ils: int, cur: str, st):
    shown = from_ils(amount_ils, cur, st["rates"])
    sym = CURRENCY_SYMBOL.get(cur, "")
    return f"{shown} {sym}" if cur == "ILS" else f"{sym}{shown}"

def fmt(amount_ils: int, st):
    return fmt_in(amount_ils, st["display_currency"], st)

def guess_category(description: str):
    d = (description or "").lower()
    for kw, cat in CATEGORY_MAP.items():
        if kw in d:
            return cat
    return "אחר"

# ===== Routes =====
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

    # ---- load user & active trip ----
    user = load_user(from_number)
    active_code = user.get("active_trip")
    if not active_code or active_code.startswith("SELF:"):
        active_code, st = ensure_self_trip(from_number)
        user["active_trip"] = active_code
        save_user(from_number, user)
    else:
        st = load_trip(active_code)
        if st is None:  # אם נמחק/לא קיים – נחזור לאישי
            active_code, st = ensure_self_trip(from_number)
            user["active_trip"] = active_code
            save_user(from_number, user)

    expenses = st["expenses"]
    log.info("Incoming | From=%s | Trip=%s | Body=%r", from_number, active_code, body_raw)

    # ===== Group commands =====
    if text.startswith("פתח קבוצה"):
        name = re.sub(r"^פתח קבוצה[:\s]*", "", body_raw).strip() or "טיול"
        code = random_code()
        trip_code = code  # נשמור בלי prefix ב-redis key יש prefix
        new_st = default_state()
        new_st["destination"] = name
        new_st["members"] = [from_number]
        new_st["code"] = trip_code
        save_trip(trip_code, new_st)
        user["active_trip"] = trip_code
        save_user(from_number, user)
        return tw_reply(f"🎉 נוצרה קבוצה: {name}\nקוד הצטרפות: {trip_code}\nשתפו את הקוד → 'הצטרף {trip_code}'")

    if text.startswith("הצטרף"):
        m = re.search(r"\b([A-Za-z0-9]{4,10})\b", body_raw)
        if not m:
            return tw_reply("לא הבנתי את הקוד 😅 נסי: 'הצטרף ABC123'")
        code = m.group(1).upper()
        if not trip_exists(code):
            return tw_reply("הקוד לא נמצא 🤔 תבדקי שלא התבלבלת באות/ספרה.")
        st2 = load_trip(code)
        st2.setdefault("members", [])
        if from_number not in st2["members"]:
            st2["members"].append(from_number)
            save_trip(code, st2)
        user["active_trip"] = code
        save_user(from_number, user)
        return tw_reply(f"✨ הצטרפת לקבוצה! יעד: {st2.get('destination') or 'ללא'}\nחברי קבוצה: {len(st2['members'])}\nאפשר להגדיר תקציב/להוסיף הוצאות כרגיל.")

    if text.startswith("החלף קבוצה"):
        m = re.search(r"\b([A-Za-z0-9]{4,10})\b", body_raw)
        if not m:
            return tw_reply("לא הבנתי את הקוד 😅 נסי: 'החלף קבוצה ABC123'")
        code = m.group(1).upper()
        if not trip_exists(code):
            return tw_reply("הקוד לא נמצא 🤔")
        user["active_trip"] = code
        save_user(from_number, user)
        return tw_reply(f"בוצע ✅ עברנו לקבוצה {code}")

    if text in ["התנתק", "התנתק מקבוצה"]:
        code, st_self = ensure_self_trip(from_number)
        user["active_trip"] = code
        save_user(from_number, user)
        return tw_reply("נותקת מהקבוצה. חזרת לטיול אישי 🧘‍♀️")

    if text in ["מי בקבוצה", "חברי קבוצה"]:
        members = st.get("members", [])
        if not members:
            return tw_reply("אין עדיין חברים בקבוצה הזו 🙂")
        shown = [m.replace("whatsapp:", "") for m in members]
        return tw_reply("👯 חברי קבוצה:\n" + "\n".join(f"• {s}" for s in shown))

    # ===== Core commands (work on active trip state) =====

    # Reset (of active trip)
    if text in ["איפוס", "reset", "start", "התחלה"]:
        st = default_state()
        st["members"] = [from_number] if active_code.startswith("SELF:") else st.get("members", []) or [from_number]
        st["code"] = active_code
        save_trip(active_code, st)
        return tw_reply("🔄 אופסנו הכול! יואוו איזה כיף להתחיל נקי ✨\nכתבי: תקציב 3000  או  יעד: אתונה\nטיפ: אפשר גם \"מטבע: דולר/יורו/שקל\"")

    # Display currency
    if text.startswith("מטבע"):
        try:
            word = body_raw.split(":", 1)[1].strip()
            cur = normalize_currency(word) or detect_currency_from_text(word, st["display_currency"])
            if cur not in ["ILS", "USD", "EUR"]:
                raise ValueError()
            st["display_currency"] = cur
            save_trip(active_code, st)
            return tw_reply(f"💱 מעכשיו מציגות ב־{cur} ({CURRENCY_SYMBOL.get(cur,'')}).\nשערים: USD={st['rates']['USD']} | EUR={st['rates']['EUR']}\n(אפשר לשנות: \"שער: USD=3.65, EUR=3.95\")")
        except Exception:
            return tw_reply('לא הבנתי? נסי: "מטבע: דולר" / "מטבע: יורו" / "מטבע: שקל"')

    # Rates
    if text.startswith("שער"):
        try:
            rhs = body_raw.split(":", 1)[1]
            pairs = re.findall(r"(USD|EUR|ILS)\s*=\s*([\d\.]+)", rhs, re.IGNORECASE)
            if not pairs: raise ValueError()
            for cur, rate in pairs:
                st["rates"][cur.upper()] = float(rate)
            save_trip(active_code, st)
            return tw_reply(f"עודכן 👍 שערים: USD={st['rates']['USD']} | EUR={st['rates']['EUR']} | ILS=1")
        except Exception:
            return tw_reply('לא הבנתי? נסי: "שער: USD=3.7" או "שער: USD=3.65, EUR=3.95"')

    # Destination
    if text.startswith("יעד"):
        try:
            dest = re.sub(r"^יעד[:\s]*", "", body_raw, flags=re.IGNORECASE).strip()
            if not dest: raise ValueError()
            st["destination"] = dest
            save_trip(active_code, st)
            return tw_reply(f"✈️ יעד נקבע: {dest} — יואוו איזה כיף! 😍")
        except Exception:
            return tw_reply('לא הבנתי? נסי כך: "יעד: לונדון"')

    # Budget
    if text.startswith("תקציב"):
        try:
            val_part = re.sub(r"^תקציב[:\s]*", "", body_raw, flags=re.IGNORECASE).strip()
            cur = detect_currency_from_text(val_part, st["display_currency"])
            amount = parse_amount(val_part)
            amount_ils = to_ils(amount, cur, st["rates"])

            st["budget"] = amount_ils
            st["remaining"] = amount_ils
            st["expenses"] = []
            st["display_currency"] = cur
            save_trip(active_code, st)

            src_sym = CURRENCY_SYMBOL.get(cur, "")
            src_txt = f"{src_sym}{amount}" if cur != "ILS" else f"{amount} ₪"
            return tw_reply(f"💰 הוגדר תקציב {fmt(amount_ils, st)} (מקור: {src_txt}).\nנשאר: {fmt(st['remaining'], st)}")
        except Exception:
            return tw_reply('לא הבנתי? נסי: "תקציב 3000" / "תקציב $2000" / "תקציב 1500€"')

    # Conversion
    if "כמה זה" in text:
        try:
            amount = parse_amount(body_raw)
            src_cur = detect_currency_from_text(body_raw, st["display_currency"])
            tgt_cur = detect_target_currency(body_raw) or st["display_currency"]
            amount_ils = to_ils(amount, src_cur, st["rates"])
            converted = fmt_in(amount_ils, tgt_cur, st)
            src_sym = CURRENCY_SYMBOL.get(src_cur, "")
            src_txt = f"{src_sym}{amount}" if src_cur != "ILS" else f"{amount} ₪"
            return tw_reply(f"{src_txt} ≈ {converted} לפי שערים: USD={st['rates']['USD']}, EUR={st['rates']['EUR']}")
        except Exception:
            return tw_reply('לא הבנתי? דוגמאות: "כמה זה 50$ בשקלים?" / "כמה זה 200 ₪ בדולרים?" / "כמה זה 30€ בשקלים?"')

    # Delete last
    if body_raw == "מחק אחרון":
        if expenses:
            last = expenses.pop()
            st["remaining"] += last["amt_ils"]
            save_trip(active_code, st)
            nice = "תתחדשי! ✨" if last["cat"] == "קניות" else ("בתיאבון 😋" if last["cat"] == "אוכל" else "סעילה נעימה 🧳" if last["cat"] in ["תחבורה","לינה"] else "👌")
            return tw_reply(f"❌ נמחקה הוצאה: {fmt(last['amt_ils'], st)} – {last['desc']} ({last['cat']})\nיתרה: {fmt(st['remaining'], st)}\n{nice}")
        else:
            return tw_reply("אין מה למחוק 🗑️")

    # Delete by amount
    if text.startswith("מחק "):
        try:
            cur = detect_currency_from_text(body_raw, st["display_currency"])
            amount = parse_amount(body_raw)
            target_ils = to_ils(amount, cur, st["rates"])
            for i in range(len(expenses) - 1, -1, -1):
                if expenses[i]["amt_ils"] == target_ils:
                    it = expenses.pop(i)
                    st["remaining"] += target_ils
                    save_trip(active_code, st)
                    return tw_reply(f"❌ נמחקה הוצאה: {fmt(target_ils, st)} – {it['desc']} ({it['cat']})\nנשאר: {fmt(st['remaining'], st)}")
            return tw_reply(f"לא מצאתי הוצאה בסך {fmt(target_ils, st)} 🤷‍♀️")
        except Exception:
            return tw_reply('לא הבנתי? נסי: "מחק 120" / "מחק $10" / "מחק 8€"')

    # Update
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
                if expenses[i]["amt_ils"] == old_ils:
                    desc = expenses[i]["desc"]; cat = expenses[i]["cat"]; who = expenses[i].get("added_by", "")
                    expenses[i] = {"amt_ils": new_ils, "desc": desc, "cat": cat, "added_by": who}
                    st["remaining"] += (old_ils - new_ils)
                    save_trip(active_code, st)
                    note = f"\n⚠️ כרגע במינוס {fmt(abs(st['remaining']), st)}" if st["remaining"] < 0 else ""
                    return tw_reply(f"✏️ עודכן: {fmt(old_ils, st)} → {fmt(new_ils, st)} ({cat})\nנשאר: {fmt(st['remaining'], st)}{note}")
            return tw_reply(f"לא מצאתי הוצאה של {fmt(old_ils, st)} לעדכן 🧐")
        except Exception:
            return tw_reply('לא הבנתי? נסי: "עדכן 50 ל-70" / "עדכן $12 ל-$9" / "עדכן 10€ ל-8€"')

    # Summary
    if body_raw in ["סיכום", "הוצאות"]:
        if expenses:
            lines = []
            by_cat = {}
            total_ils = 0
            for it in expenses:
                who = it.get("added_by", "")
                who = who.replace("whatsapp:", "") if who else ""
                lines.append(f"- {fmt(it['amt_ils'], st)} – {it['desc']} ({it['cat']})" + (f" • {who}" if who else ""))
                total_ils += it["amt_ils"]
                by_cat[it["cat"]] = by_cat.get(it["cat"], 0) + it["amt_ils"]

            cat_lines = [f"{cat}: {fmt(val, st)}" for cat, val in sorted(by_cat.items(), key=lambda x: -x[1])]
            msg = []
            msg.append("📊 סיכום חמוד:")
            msg.extend(lines)
            msg.append(f"\nסה\"כ הוצאות: {fmt(total_ils, st)}")
            msg.append(f"יתרה: {fmt(st['remaining'], st)}" + (f"  ⚠️ מינוס {fmt(abs(st['remaining']), st)}" if st["remaining"] < 0 else ""))
            if st["budget"] > 0: msg.append(f"תקציב: {fmt(st['budget'], st)}")
            if st["destination"]: msg.append(f"יעד: {st['destination']}")
            msg.append("\nלפי קטגוריות:")
            msg.extend(cat_lines)
            msg.append(f"\nחברי קבוצה: {len(st.get('members', []))}")
            return tw_reply("\n".join(msg))
        else:
            base = f"יתרה: {fmt(st['remaining'], st)}"
            if st["remaining"] < 0: base += f"  ⚠️ מינוס {fmt(abs(st['remaining']), st)}"
            return tw_reply("עדיין לא נרשמו הוצאות.\n" + base)

    # Add expense
    if any(ch.isdigit() for ch in body_raw):
        if st["budget"] == 0:
            return tw_reply("📝 קודם מגדירות תקציב, סיס! נסי: תקציב 3000 או תקציב $2000")
        try:
            cleaned = re.sub(r"^הוצאה[:\s]*", "", body_raw, flags=re.IGNORECASE).strip()
            cur = detect_currency_from_text(cleaned, st["display_currency"])
            amt = parse_amount(cleaned)
            amt_ils = to_ils(amt, cur, st["rates"])

            after_number = re.split(r"\d[\d,\.]*", cleaned, maxsplit=1)
            desc = after_number[1].strip() if len(after_number) > 1 else ""
            desc = re.sub(r"^\s*(דולר|יורו|אירו|שקל|ש\"ח|₪|\$|€)\s*", "", desc, flags=re.IGNORECASE)
            if not desc:
                if "–" in cleaned:
                    desc = cleaned.split("–", 1)[1].strip()
                elif "-" in cleaned:
                    desc = cleaned.split("-", 1)[1].strip()
                if not desc:
                    desc = "הוצאה"

            cat = guess_category(desc)

            expenses.append({"amt_ils": amt_ils, "desc": desc, "cat": cat, "added_by": from_number})
            st["remaining"] -= amt_ils
            save_trip(active_code, st)

            extra = ""
            if cat == "אוכל": extra = " בתיאבון! 😋"
            elif cat == "קניות": extra = " תתחדשי! ✨"
            elif cat in ["תחבורה", "לינה"]: extra = " נסיעה טובה! 🧳"
            note = f"\n⚠️ כרגע במינוס {fmt(abs(st['remaining']), st)}" if st["remaining"] < 0 else ""

            return tw_reply(f"➕ נוספה הוצאה: {fmt(amt_ils, st)} – {desc} ({cat})\nנשאר: {fmt(st['remaining'], st)}{note}{extra}")

        except Exception as e:
            logging.exception("add-expense failed: %s", e)
            return tw_reply("לא הצלחתי להבין את ההוצאה 😅\nדוגמאות:\n• הוצאה 20$ – פיצה\n• 20 דולר פיצה\n• 120 – שמלה\n• 15€ – קפה")

    # Unknown command
    return tw_reply("לא הבנתי עדיין 🫣 נסי לנסח כך:\n"
                    "• פתח קבוצה אתונה  |  הצטרף ABC123  |  מי בקבוצה\n"
                    "• תקציב 3000  |  תקציב $2000\n"
                    "• הוצאה 50₪ – קפה  |  20 דולר פיצה  |  120 – שמלה\n"
                    "• סיכום  |  מחק אחרון  |  מחק 120  |  עדכן 50 ל-70\n"
                    "• יעד: לונדון  |  מטבע: דולר  |  שער: USD=3.65")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
