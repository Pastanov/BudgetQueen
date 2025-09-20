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
            health_check_interval=30,
            retry_on_timeout=True,
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

def default_state():
    return {
        "budget": 0,
        "remaining": 0,
        "destination": "",
        "expenses": [],  # {amt_ils:int, desc:str, cat:str, added_by:str}
        "rates": DEFAULT_RATES.copy(),
        "display_currency": "ILS",
        "members": [],
        "names": {},     # phone -> name
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
        try: return bool(r.exists(key))
        except: return False
    return key in MEM_TRIPS

def load_trip(code):
    key = trip_key(code)
    if USE_REDIS:
        try:
            raw = r.get(key)
            if raw: return json.loads(raw)
            return None
        except: return None
    return MEM_TRIPS.get(key)

def save_trip(code, st):
    key = trip_key(code)
    if USE_REDIS:
        try: r.set(key, json.dumps(st)); return
        except Exception as e: log.warning("Redis write failed for trip %s: %s", code, e)
    MEM_TRIPS[key] = st

def load_user(num):
    key = user_key(num)
    if USE_REDIS:
        try:
            raw = r.get(key)
            if raw: return json.loads(raw)
        except: pass
    return MEM_USERS.get(key, {"active_trip": f"SELF:{num}"})

def save_user(num, meta):
    key = user_key(num)
    if USE_REDIS:
        try: r.set(key, json.dumps(meta)); return
        except: pass
    MEM_USERS[key] = meta

def ensure_self_trip(num):
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
        if k in t.lower(): return v
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

def parse_first_amount(text: str):
    m = re.search(r"(\d[\d,\.]*)", text)
    if not m: raise ValueError("no number")
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
        if kw in d: return cat
    return "אחר"

def short_phone(p):
    p = p.replace("whatsapp:", "")
    return p[:-4].rjust(len(p)-4, "•") + p[-4:] if len(p) >= 4 else p

def display_name(phone, st):
    return st.get("names", {}).get(phone) or short_phone(phone)

# ===== Routes =====
@app.route("/", methods=["GET"])
def home():
    return "Budget Queen WhatsApp Bot - OK", 200

@app.route("/whatsapp", methods=["GET", "POST"])
def whatsapp():
    if request.method == "GET":
        return "Webhook is ready", 200

    # ---- keep Redis alive (reconnect if needed) ----
    global r, USE_REDIS
    if r is not None:
        try:
            r.ping()
        except Exception:
            try:
                from redis import Redis
                r = Redis.from_url(os.getenv("REDIS_URL"), decode_responses=True,
                                   socket_connect_timeout=5, socket_timeout=5,
                                   health_check_interval=30, retry_on_timeout=True)
                r.ping()
                USE_REDIS = True
            except Exception as _:
                log.warning("Redis temporarily unavailable; continuing in-memory")
                USE_REDIS = False

    try:
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
            if st is None:
                active_code, st = ensure_self_trip(from_number)
                user["active_trip"] = active_code
                save_user(from_number, user)

        expenses = st["expenses"]
        st.setdefault("names", {})
        log.info("Incoming | From=%s | Trip=%s | Body=%r", from_number, active_code, body_raw)

        # ===== Group commands =====
        if text.startswith("פתח קבוצה"):
            name = re.sub(r"^פתח קבוצה[:\s]*", "", body_raw).strip() or "טיול"
            code = random_code()
            new_st = default_state()
            new_st["destination"] = name
            new_st["members"] = [from_number]
            new_st["names"][from_number] = new_st["names"].get(from_number, "אני")
            new_st["code"] = code
            save_trip(code, new_st)
            user["active_trip"] = code
            save_user(from_number, user)
            return tw_reply(f"🎉 נוצרה קבוצה: {name}\nקוד הצטרפות: {code}\nשתפו את הקוד → 'הצטרף {code}'")

        if text.startswith("הצטרף"):
            m = re.search(r"\b([A-Za-z0-9]{4,10})\b", body_raw)
            if not m: return tw_reply("לא הבנתי את הקוד 😅 נסי: 'הצטרף ABC123'")
            code = m.group(1).upper()
            if not trip_exists(code): return tw_reply("הקוד לא נמצא 🤔")
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
            if not m: return tw_reply("לא הבנתי את הקוד 😅 נסי: 'החלף קבוצה ABC123'")
            code = m.group(1).upper()
            if not trip_exists(code): return tw_reply("הקוד לא נמצא 🤔")
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
            if not members: return tw_reply("אין עדיין חברים בקבוצה הזו 🙂")
            shown = [display_name(m, st) for m in members]
            return tw_reply("👯 חברי קבוצה:\n" + "\n".join(f"• {s}" for s in shown))

        # ===== Names =====
        if text.startswith("שם:") or text.startswith("שם :"):
            name = body_raw.split(":", 1)[1].strip()
            if not name: return tw_reply('לא הבנתי? נסי: "שם: נוי"')
            st["names"][from_number] = name
            save_trip(active_code, st)
            return tw_reply(f"נעים להכיר {name}! 🥰 נשמור את זה לסיכומים.")

        if text.startswith("שם "):
            m = re.match(r"שם\s+(\+?\d+)\s*:\s*(.+)$", body_raw)
            if m:
                num, name = m.group(1), m.group(2).strip()
                st["names"]["whatsapp:"+num if not num.startswith("whatsapp:") else num] = name
                save_trip(active_code, st)
                return tw_reply(f"בוצע ✅ שמרתי את {name}")

        if text.startswith("שמות"):
            try:
                rhs = body_raw.split(":", 1)[1]
                given = [s.strip() for s in re.split(r"[,\n]", rhs) if s.strip()]
                if not given: raise ValueError()
                members = st.get("members", [])
                for i, mbr in enumerate(members):
                    if i < len(given):
                        st["names"][mbr] = given[i]
                save_trip(active_code, st)
                return tw_reply("שמות עודכנו ✨ (לפי סדר 'מי בקבוצה').")
            except Exception:
                return tw_reply('לא הבנתי? נסי: "שמות: נוי, יובל, …"')

        # ===== Core commands =====
        if text in ["איפוס", "reset", "start", "התחלה"]:
            st = default_state()
            st["members"] = [from_number] if active_code.startswith("SELF:") else st.get("members", []) or [from_number]
            st["names"][from_number] = st["names"].get(from_number, "אני")
            st["code"] = active_code
            save_trip(active_code, st)
            return tw_reply("🔄 אופסנו הכול! יואוו איזה כיף להתחיל נקי ✨\nכתבי: תקציב 3000  או  יעד: אתונה\nטיפ: אפשר גם \"מטבע: דולר/יורו/שקל\"")

        if text.startswith("מטבע"):
            try:
                word = body_raw.split(":", 1)[1].strip()
                cur = normalize_currency(word) or detect_currency_from_text(word, st["display_currency"])
                if cur not in ["ILS", "USD", "EUR"]: raise ValueError()
                st["display_currency"] = cur
                save_trip(active_code, st)
                return tw_reply(f"💱 מעכשיו מציגות ב־{cur} ({CURRENCY_SYMBOL.get(cur,'')}).\nשערים: USD={st['rates']['USD']} | EUR={st['rates']['EUR']}")
            except Exception:
                return tw_reply('לא הבנתי? נסי: "מטבע: דולר" / "מטבע: יורו" / "מטבע: שקל"')

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

        if text.startswith("יעד"):
            try:
                dest = re.sub(r"^יעד[:\s]*", "", body_raw, flags=re.IGNORECASE).strip()
                if not dest: raise ValueError()
                st["destination"] = dest
                save_trip(active_code, st)
                return tw_reply(f"✈️ יעד נקבע: {dest} — יואוו איזה כיף! 😍")
            except Exception:
                return tw_reply('לא הבנתי? נסי כך: "יעד: לונדון"')

        if text.startswith("תקציב"):
            try:
                val_part = re.sub(r"^תקציב[:\s]*", "", body_raw, flags=re.IGNORECASE).strip()
                cur = detect_currency_from_text(val_part, st["display_currency"])
                amount = parse_first_amount(val_part)
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

        if "כמה זה" in text:
            try:
                amount = parse_first_amount(body_raw)
                src_cur = detect_currency_from_text(body_raw, st["display_currency"])
                tgt_cur = detect_target_currency(body_raw) or st["display_currency"]
                amount_ils = to_ils(amount, src_cur, st["rates"])
                converted = fmt_in(amount_ils, tgt_cur, st)
                src_sym = CURRENCY_SYMBOL.get(src_cur, "")
                src_txt = f"{src_sym}{amount}" if src_cur != "ILS" else f"{amount} ₪"
                return tw_reply(f"{src_txt} ≈ {converted} לפי שערים: USD={st['rates']['USD']}, EUR={st['rates']['EUR']}")
            except Exception:
                return tw_reply('לא הבנתי? דוגמאות: "כמה זה 50$ בשקלים?" / "כמה זה 200 ₪ בדולרים?" / "כמה זה 30€ בשקלים?"')

        # ===== Delete (smart) =====
        if text.startswith("מחק "):
            q = body_raw[4:].strip()

            def norm(s):
                s = s.lower()
                s = re.sub(r"[^\w\u0590-\u05FF ]+", " ", s)
                s = re.sub(r"\s+", " ", s).strip()
                return s

            if re.fullmatch(r"\d+", q):
                idx = int(q) - 1
                if 0 <= idx < len(expenses):
                    it = expenses.pop(idx)
                    st["remaining"] += it["amt_ils"]
                    save_trip(active_code, st)
                    who = display_name(it.get("added_by",""), st) if it.get("added_by") else ""
                    return tw_reply(
                        f"❌ נמחקה הוצאה #{idx+1}: {fmt(it['amt_ils'], st)} – {it['desc']} ({it['cat']})"
                        + (f" • {who}" if who else "")
                        + f"\nנשאר: {fmt(st['remaining'], st)}"
                    )
                return tw_reply("לא מצאתי פריט עם האינדקס הזה 🤷‍♀️")

            try:
                cur = detect_currency_from_text(q, st["display_currency"])
                amount = parse_first_amount(q)
                target_ils = to_ils(amount, cur, st["rates"])
                for i in range(len(expenses) - 1, -1, -1):
                    if expenses[i]["amt_ils"] == target_ils:
                        it = expenses.pop(i)
                        st["remaining"] += target_ils
                        save_trip(active_code, st)
                        who = display_name(it.get("added_by",""), st) if it.get("added_by") else ""
                        return tw_reply(
                            f"❌ נמחקה הוצאה: {fmt(target_ils, st)} – {it['desc']} ({it['cat']})"
                            + (f" • {who}" if who else "")
                            + f"\nנשאר: {fmt(st['remaining'], st)}"
                        )
            except Exception:
                pass

            qn = norm(q)
            matches = []
            for i in range(len(expenses) - 1, -1, -1):
                if qn and qn in norm(expenses[i]["desc"]):
                    matches.append(i)

            if matches:
                i = matches[0]
                it = expenses.pop(i)
                st["remaining"] += it["amt_ils"]
                save_trip(active_code, st)
                who = display_name(it.get("added_by",""), st) if it.get("added_by") else ""
                return tw_reply(
                    f"❌ נמחקה הוצאה: {fmt(it['amt_ils'], st)} – {it['desc']} ({it['cat']})"
                    + (f" • {who}" if who else "")
                    + f"\nנשאר: {fmt(st['remaining'], st)}"
                )

            if expenses:
                dn_list = [norm(e["desc"]) for e in expenses]
                uniq = []
                for dn in dn_list:
                    if dn and dn not in uniq:
                        uniq.append(dn)
                    if len(uniq) >= 5:
                        break
                if uniq:
                    hints = " / ".join(uniq)
                    return tw_reply("לא מצאתי מה למחוק 😅\nטיפים: 'מחק 2' (אינדקס) / 'מחק 11$' / נסי מילה ייחודית מתוך התיאור.\nלמשל: " + hints)
            return tw_reply("לא מצאתי מה למחוק 😅\nטיפים: 'מחק 2' (אינדקס) / 'מחק 11$' / 'מחק משחק'")

        if body_raw == "מחק אחרון":
            if expenses:
                last = expenses.pop()
                st["remaining"] += last["amt_ils"]
                save_trip(active_code, st)
                who = display_name(last.get("added_by",""), st) if last.get("added_by") else ""
                return tw_reply(f"❌ נמחקה הוצאה אחרונה: {fmt(last['amt_ils'], st)} – {last['desc']} ({last['cat']})"
                                + (f" • {who}" if who else "") + f"\nיתרה: {fmt(st['remaining'], st)}")
            else:
                return tw_reply("אין מה למחוק 🗑️")

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

        if body_raw in ["סיכום", "הוצאות"]:
            if expenses:
                lines = []
                by_cat = {}
                total_ils = 0
                for idx, it in enumerate(expenses, start=1):
                    who = display_name(it.get("added_by",""), st) if it.get("added_by") else ""
                    lines.append(f"{idx}. {fmt(it['amt_ils'], st)} – {it['desc']} ({it['cat']})" + (f" • {who}" if who else ""))
                    total_ils += it["amt_ils"]
                    by_cat[it["cat"]] = by_cat.get(it["cat"], 0) + it["amt_ils"]

                cat_lines = [f"{cat}: {fmt(val, st)}" for cat, val in sorted(by_cat.items(), key=lambda x: -x[1])]
                msg = []
                msg.append("📊 סיכום חמוד:")
                msg.extend("• " + ln for ln in lines)
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

        # ===== Add expense (first number rule) =====
        if any(ch.isdigit() for ch in body_raw):
            if st["budget"] == 0:
                return tw_reply("📝 קודם מגדירות תקציב, סיס! נסי: תקציב 3000 או תקציב $2000")
            try:
                cleaned = re.sub(r"^הוצאה[:\s]*", "", body_raw, flags=re.IGNORECASE).strip()

                m = re.search(r"(\d[\d,\.]*)", cleaned)
                if not m: raise ValueError("no number")
                num_span_end = m.end()
                cur = detect_currency_from_text(cleaned, st["display_currency"])
                amt = parse_first_amount(cleaned)
                amt_ils = to_ils(amt, cur, st["rates"])

                desc = cleaned[num_span_end:].strip()
                desc = re.sub(r"^[\s\-–:.,]*(דולר|יורו|אירו|שקל|ש\"ח|₪|\$|€)?[\s\-–:.,]*", "", desc, flags=re.IGNORECASE)
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

        # ===== Unknown =====
        return tw_reply("לא הבנתי עדיין 🫣 נסי לנסח כך:\n"
                        "• פתח קבוצה אתונה  |  הצטרף ABC123  |  מי בקבוצה | שמות: נוי, יובל\n"
                        "• שם: נוי  |  שם +9725xxxxxxx: יובל\n"
                        "• תקציב 3000  |  תקציב $2000\n"
                        "• הוצאה 50₪ – קפה  |  20 דולר פיצה  |  120 – שמלה\n"
                        "• סיכום  |  מחק אחרון  |  מחק 2  |  מחק 11$  |  מחק משחק\n"
                        "• יעד: לונדון  |  מטבע: דולר  |  שער: USD=3.65")

    except Exception as e:
        log.exception("Unhandled error in /whatsapp: %s", e)
        return tw_reply("אופס, קרתה תקלה רגעית 😅 נסי שוב עוד שניה.\nאם זה חוזר—שלחי 'סיכום' לוודא שהכל שמור 🙏")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
