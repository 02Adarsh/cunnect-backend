import base64
import hashlib
import inspect
import json
import re
import threading
import time

import requests

from concurrent.futures import ThreadPoolExecutor, as_completed

from html import escape as html_escape

from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse

from .scraper_backend import CUIMSScraperBackend

VIEWS_VERSION = "5.17-showallsem"
print(f"[Views] views v{VIEWS_VERSION} loaded")

DEFAULT_BASE_URL = "https://student.culko.in"

# ⭐ Login/enter-password background media (static folder ke andar ka path).
# .mp4 rakho to template VIDEO tag se chalayega; image path rakho to
# wahi old background-image div fallback dikhega.
LOGIN_BG_MEDIA = "videos/login-bg.mp4"


_TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})")


def _meridiem(text):
    cleaned = str(text).replace(".", "")
    if "PM" in cleaned:
        return "PM"
    if "AM" in cleaned:
        return "AM"
    return None


def _to_minutes(hour, minute, meridiem):
    if meridiem == "PM" and hour < 12:
        hour += 12
    elif meridiem == "AM" and hour == 12:
        hour = 0
    elif meridiem is None and 1 <= hour <= 6:
        # Unmarked 1-6 o'clock in a college day means the afternoon block.
        hour += 12
    return hour * 60 + minute


def _parse_time_range(raw):
    """Parse a "start - end" range into (start_minutes, end_minutes).

    The portal prints the meridiem only at the END of the range, e.g.
    "11:20 - 12:10 PM" actually means 11:20 AM - 12:10 PM. So when the
    start has no meridiem of its own we try, in order: end's meridiem,
    plain 24h, AM, PM - and pick the first that does not exceed the end.
    """
    parts = re.split(r"[-–—]|\bTO\b", str(raw or "").upper().strip())

    start_match = _TIME_RE.search(parts[0])
    if not start_match:
        return None
    sh, smin = int(start_match.group(1)), int(start_match.group(2))
    if sh > 23 or smin > 59:
        return None

    start_minutes = None
    end_minutes = None
    start_meridiem = _meridiem(parts[0])

    if len(parts) > 1:
        end_match = _TIME_RE.search(parts[1])
        if end_match:
            eh, emin = int(end_match.group(1)), int(end_match.group(2))
            if eh <= 23 and emin <= 59:
                end_meridiem = _meridiem(parts[1])
                end_minutes = _to_minutes(eh, emin, end_meridiem)
                if start_meridiem is not None:
                    start_minutes = _to_minutes(sh, smin, start_meridiem)
                else:
                    candidates = (
                        [end_meridiem] if end_meridiem else []
                    ) + [None, "AM", "PM"]
                    for candidate in candidates:
                        value = _to_minutes(sh, smin, candidate)
                        if value <= end_minutes:
                            start_minutes = value
                            break
                    if start_minutes is None:
                        start_minutes = end_minutes

    if start_minutes is None:
        start_minutes = _to_minutes(sh, smin, start_meridiem)
    if end_minutes is None:
        end_minutes = start_minutes + 60
    return start_minutes, end_minutes


def _att_normalize_key(key):
    return re.sub(r"[\s_.\-]+", "", str(key)).lower()


def _att_number(record, candidates, as_float=False):
    """⭐ Portal attendance record se numeric value - KEY-AGNOSTIC.

    Portal ke GetReport JSON ke exact key names campus/instance ke
    hisaab se badalte hain (Total_Pres / Present / "Total Present" ...).
    Isliye saare keys normalize karke candidates list se match karte
    hain - jo pehle numeric mile wahi lo. Kuch na mile to 0.
    """
    normalized = {}
    for key, value in record.items():
        normalized[_att_normalize_key(key)] = value
    for cand in candidates:
        if cand in normalized:
            try:
                num = float(str(normalized[cand]).strip())
                return num if as_float else int(num)
            except (TypeError, ValueError):
                continue
    return 0.0 if as_float else 0


# ⭐ Attendance JSON ke possible key names (normalized - lowercase, no spaces)
# culko portal ka asli format: Total_Attd / Total_Delv / Total_Perc
_ATT_PRESENT_KEYS = (
    "totalattd", "eligibilityattended",  # ⭐ culko portal ke asli keys
    "totalpres", "totalpre", "totalp", "present", "attended", "totalpresent",
    "presentlec", "presentlectures", "totalclassesattended",
)
_ATT_TOTAL_KEYS = (
    "totaldelv", "eligibilitydelivered",  # ⭐ culko portal ke asli keys
    "delivered", "totalatt", "totaldelivered", "totalconducted",
    "conducted", "held", "totallectures", "totalclasses",
    "classesconducted", "totala", "total",
)
_ATT_PCT_KEYS = (
    "totalpercentage", "totalperc", "percentage", "percent", "per",
    "attendancepercentage",
)


def clean_attendance_records(records):
    """⭐ Portal ke raw attendance JSON -> template-ready records.

    authenticate_view (login) aur dashboard_data_view (realtime sync)
    DONO yahi use karte hain - mapping ek jagah, kabhi hairaat nahi.
    """
    cleaned = []
    for record in records or []:
        title = str(
            record.get("Title")
            or record.get("Course")
            or record.get("CourseName")
            or record.get("Subject")
            or "Unknown Course"
        )
        # ⭐ Portal ke raw record me alag se "Code" key hota hai -
        # pehle wahi lo ("CODE : Title" colon-split sirf fallback hai).
        code = str(record.get("Code") or record.get("CourseCode") or "").strip()
        course_title = title
        if ":" in title:
            t_code, t_title = title.split(":", 1)
            if not code:
                code = t_code.strip()
            course_title = t_title.strip() or course_title
        # ⭐ "CODE - Title" / "CODE – Title" (ya title ke start me hi
        # code aa gaya) to title se code-prefix hata do - warna title
        # me code dobara repeat hota.
        if code and course_title.upper().startswith(code.upper()):
            rest = course_title[len(code):].lstrip(" :-–—")
            if rest:
                course_title = rest

        attended = _att_number(record, _ATT_PRESENT_KEYS)
        total = _att_number(record, _ATT_TOTAL_KEYS)
        percentage = _att_number(record, _ATT_PCT_KEYS, as_float=True)

        if percentage >= 75:
            miss = max(0, int((100 * attended - 75 * total) // 75))
            need = 0
        else:
            miss = 0
            need = max(0, int(((75 * total - 100 * attended) + 24) // 25))

        cleaned.append({
            "code": code,
            "title": course_title,
            "attended": attended,
            "total": total,
            "percentage": percentage,
            "miss": miss,
            "need": need,
        })
    return cleaned


def normalize_timetable_map(raw_timetable):
    """{Monday: [...]} -> {"MON": {"day","full_day","slots":[sorted]}}."""
    mapped = {}
    for day, slots in (raw_timetable or {}).items():
        short_day = str(day)[:3].upper()
        mapped[short_day] = {
            "day": short_day,
            "full_day": str(day).upper(),
            "slots": sort_slots_by_time(slots),
        }
    return mapped


def build_dashboard_sig(request):
    """⭐ Portal data ka lightweight signature (16 hex).

    JS har poll pe ye compare karta hai: timetable / notices / fees /
    attendance-COUNT badle to sirf tab auto-reload (numbers in-place
    update hote hain, reload ki zaroorat nahi hoti unke liye).
    ⭐ v5.2: day-wise P/A (daily attendance) bhi sig me - teacher portal
    pe attendance chadha de to data-view usi waqt daily rescrape karta
    hai + sig badalne se page 1 baar auto-reload hota hai -> course
    modal ki Timeline bhi REALTIME fresh (user request).
    """
    timetable = request.session.get("timetable_data") or {}
    notices = request.session.get("announcements") or []
    fees = request.session.get("fee_records") or []
    records = request.session.get("attendance_data") or []
    daily = request.session.get("daily_attendance") or {}
    dstats = daily.get("stats") or {}
    slim_notices = [
        [str(n.get("title", ""))[:60], str(n.get("date", ""))]
        for n in notices
    ]
    slim_fees = [
        str(f.get("receipt", "")) + "|" + str(f.get("date", ""))
        for f in fees
    ]
    # ⭐ v5.16-resultsync: results ka state bhi sig me - pending semester
    # ka result portal pe declare hote hi sync session refresh karti hai
    # + sig badalne se page 1 baar auto-reload -> SEM 3 BINA RELOGIN
    # apne-aap app pe dikhne lagega (user ask).
    exam_results = request.session.get("exam_results") or []
    grades = request.session.get("subject_grades") or []
    slim_results = [
        str(res.get("semester", "")) + "|" + str(res.get("sgpa", ""))
        for res in exam_results
    ]
    blob = json.dumps(
        {
            "tt": timetable,
            "n": slim_notices,
            "f": slim_fees,
            "r": len(records),
            "d": [
                daily.get("records", 0),
                dstats.get("present", 0),
                dstats.get("absent", 0),
            ],
            "res": slim_results,
            "g": len(grades),
            "pend": 1 if request.session.get("result_pending") else 0,
            "cg": str(request.session.get("student_cgpa") or ""),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


# ⭐ Realtime sync: portal ko itni hi baar hit karo (har poll pe nahi)
DASHBOARD_RESCRAPE_MIN_SECONDS = 180


# ══════════════ ⭐ PORTAL SESSION KEEPALIVE ══════════════
# Dashboard BAND hone ke baad bhi portal session zinda rakhne ke liye:
# login pe ek background daemon-thread start hota hai jo har
# KEEPALIVE_INTERVAL_SECONDS me portal ki proven-safe page
# (frmMyTimeTable.aspx - wahi health-probe wali) ko ping karta hai.
# ASP.NET idle-timeout (~20 min) se pehle 10 min ka ping = session kabhi
# bejaan nahi hota. Thread cookies module-level store me rakhta hai aur
# har ping pe rotate hui cookies wapas save karta hai - sync aur thread
# dono isi store se taza cookies uthate hain.
KEEPALIVE_INTERVAL_SECONDS = 600        # 10 min (ASP.NET timeout se pehle)
KEEPALIVE_MAX_AGE_SECONDS = None        # ⭐ None = thread KABHI retire nahi
                                        # (server restart / portal session marne
                                        #  tak ping karta rahega; personal app ke
                                        #  liye bilkul safe - thread runserver ke
                                        #  saath hi jeeta-marta hai)

_KEEPALIVE_STATE = {}   # key -> {"cookies": {...}, "alive": bool, "seen": ts, "thread": Thread}
_KEEPALIVE_LOCK = threading.Lock()


def _keepalive_key(base_url, uid):
    return f"{base_url}|{uid}"


def get_keepalive_state(base_url, uid):
    """Thread ki shared state ki ek COPY lo (bahar mutate mat karo)."""
    with _KEEPALIVE_LOCK:
        entry = _KEEPALIVE_STATE.get(_keepalive_key(base_url, uid))
        return dict(entry) if entry else {}


def update_keepalive_state(base_url, uid, cookies=None, alive=None):
    """Cookies / alive flag store me merge karo."""
    key = _keepalive_key(base_url, uid)
    with _KEEPALIVE_LOCK:
        entry = _KEEPALIVE_STATE.setdefault(key, {"alive": True})
        if cookies:
            entry["cookies"] = dict(cookies)
        if alive is not None:
            entry["alive"] = alive
        entry["seen"] = time.time()
    return entry


def _portal_keepalive_loop(base_url, uid):
    """Background loop: portal ko ping karo, cookies fresh rakho.

    Session mar jaye (Login.aspx redirect) to alive=False mark karke
    thread band - dashboard banner user ko re-login pe bhejega.
    """
    key = _keepalive_key(base_url, uid)
    scraper = CUIMSScraperBackend(base_url=base_url, uid=uid)
    started = time.time()
    print(f"[Keepalive] thread start - {uid}, {KEEPALIVE_INTERVAL_SECONDS}s interval")

    while KEEPALIVE_MAX_AGE_SECONDS is None or (
        time.time() - started < KEEPALIVE_MAX_AGE_SECONDS
    ):
        entry = get_keepalive_state(base_url, uid)
        cookies = entry.get("cookies") or {}
        if not cookies or not entry.get("alive", True):
            break
        try:
            scraper.session.cookies.clear()
            for name, value in cookies.items():
                scraper.session.cookies.set(name, value)
            probe = scraper.session.get(
                scraper.auth_url + "frmMyTimeTable.aspx",
                timeout=12,
                allow_redirects=True,
            )
            if "login" in probe.url.lower():
                print("[Keepalive] portal session EXPIRED - re-login chahiye")
                update_keepalive_state(base_url, uid, alive=False)
                break
            fresh = requests.utils.dict_from_cookiejar(scraper.session.cookies)
            update_keepalive_state(base_url, uid, cookies=fresh or cookies, alive=True)
            print("[Keepalive] ping ok - portal session zinda")
        except Exception as exc:
            # Network fail = thread band MAT karo, agle interval pe retry
            print(f"[Keepalive] ping failed (retry next interval): {exc}")
        # ⭐ Sleep BAAD me: thread shuru hote hi TURANT pehla ping (login
        # ya restart-recovery pe foran pata chalta hai session zinda hai
        # ya nahi - 10 min ka wait nahi).
        time.sleep(KEEPALIVE_INTERVAL_SECONDS)

    # ⭐ Entry delete MAT karo - alive=False flag dashboard banner ko
    # dikhana hai. Sirf thread reference hatao; next login pe entry
    # phir se seed ho jayegi.
    with _KEEPALIVE_LOCK:
        if key in _KEEPALIVE_STATE:
            _KEEPALIVE_STATE[key]["thread"] = None
    print(f"[Keepalive] thread stop - {uid}")


def start_portal_keepalive(base_url, uid, cookies):
    """Login ke baad bulao: store seed karo + thread start karo (agar
    already chal raha hai to sirf cookies refresh hongi)."""
    update_keepalive_state(base_url, uid, cookies=cookies, alive=True)
    key = _keepalive_key(base_url, uid)
    with _KEEPALIVE_LOCK:
        thread = _KEEPALIVE_STATE.get(key, {}).get("thread")
        if thread and thread.is_alive():
            return
        thread = threading.Thread(
            target=_portal_keepalive_loop,
            args=(base_url, uid),
            daemon=True,   # runserver restart/reload pe khud mar jayega
        )
        _KEEPALIVE_STATE[key]["thread"] = thread
        thread.start()


def ensure_keepalive_running(request):
    """⭐ Server restart ke baad keepalive thread WAPAS zinda karo.

    Keepalive thread runserver ki MEMORY me rehta hai - runserver
    restart hote hi mar jata hai (Django session DB/sqlite me bacha
    rehta hai, isliye dashboard to khul jaata hai, par portal-side
    session pings ke bina dheere-dheere mar jaata tha -> baar-baar
    re-login). Ab dashboard load / data poll pe check: thread nahi chal
    raha + stored cookies hain -> thread dobara start (+ restore loop ka
    turant pehla ping bata dega portal zinda hai ya re-login chahiye).

    Isse restart ke baad bhi UID + password + captcha almost kabhi
    nahi bharna padega - re-login sirf tab jab portal ne sach me
    session kaat diya ho.
    """
    try:
        state = request.session.get("scraper_state") or {}
        uid = state.get("uid") or request.session.get("student_uid")
        base_url = state.get("base_url")
        cookies = state.get("cookies") or {}
        if not (uid and base_url and cookies):
            return
        entry = get_keepalive_state(base_url, uid)
        if entry and not entry.get("alive", True):
            return  # portal ne maara hua session - revive nahi, banner dikhega
        thread = entry.get("thread")
        if thread and thread.is_alive():
            return
        start_portal_keepalive(base_url, uid, cookies)
        print(f"[Keepalive] restart-recovery - thread wapas start: {uid}")
    except Exception:
        # Kabhi bhi dashboard todna nahi chahiye
        pass


def stop_portal_keepalive(base_url, uid):
    """⭐ Logout pe bulao: alive=False + cookies clear -> keepalive loop
    agle wake pe khud band ho jayega (daemon thread hai, restart pe
    waise bhi mar jaata hai)."""
    if not (base_url and uid):
        return
    with _KEEPALIVE_LOCK:
        entry = _KEEPALIVE_STATE.get(_keepalive_key(base_url, uid))
        if entry is not None:
            entry["alive"] = False
            entry["cookies"] = {}


def sort_slots_by_time(slots):
    """Sort timetable slots chronologically by their start time.

    Handles "9:30 AM - 10:20 AM", "09:30-10:30", "13:30 - 14:30",
    "1:15 - 2:15" (unmarked 12h), "11:20 - 12:10 PM" (noon-crossing),
    "9.30 AM", "9:30 a.m." - unparsable slots sink to the bottom.
    """
    def key(slot):
        raw = (
            slot.get("time", "") if isinstance(slot, dict)
            else getattr(slot, "time", "")
        )
        parsed = _parse_time_range(raw)
        return parsed[0] if parsed else 9999

    return sorted(slots or [], key=key)


def sort_timetable_slots(timetable):
    """Sort every day's slot list of the mapped timetable, in place."""
    for item in (timetable or {}).values():
        item["slots"] = sort_slots_by_time(item.get("slots", []))
    return timetable


def sort_academic_sessions(sessions):
    """Sort portal sessions chronologically."""

    def key(item):
        session_id = str(item.get("id", "")).upper()
        session_name = str(item.get("name", "")).upper()

        prefix_match = re.match(r"^(\d+)", session_id)
        prefix = int(prefix_match.group(1)) if prefix_match else 0

        year_match = re.search(r"20\d{2}", session_id)
        year = int(year_match.group(0)) if year_match else 0

        if "ODD" in session_id or "ODD" in session_name:
            term = 1
        elif "EVEN" in session_id or "EVEN" in session_name:
            term = 2
        else:
            term = 0

        return prefix, year, term, session_id

    sessions = sessions or []

    # ⭐ Chronological ASCENDING (oldest pehle: 25261 -> 25262 -> 26271).
    # Pehle reverse (newest pehle) tha - isse 3+ sessions wale account
    # me labels ULT palt jaate the: current ongoing Sem 3 ko "Semester
    # 1" likh deta tha, aur asli Sem 1 "Semester 3"! Academic numbering
    # ab numbered_sessions() id-digits se karti hai - ye sort sirf
    # fallback order deta hai.
    if sessions and all(
        re.fullmatch(r"\d+", str(item.get("id", "")))
        for item in sessions
    ):
        return sorted(
            sessions,
            key=lambda item: int(str(item.get("id", "0"))),
        )

    return sorted(sessions, key=key)


def academic_semester_number(session_id, batch_year):
    """⭐ Session id -> ASLI academic semester number.

    Culko format: YY-start, YY-end, Term (1=Odd, 2=Even) -
    25261 = 2025-26 ODD, 25262 = 2025-26 EVEN, 26271 = 2026-27 ODD.
    Batch year UID ke first 2 digits (25LBCS3056 -> 25):
      Sem N = (startYY - batchYY) * 2 + term
    25261 -> 1, 25262 -> 2, 26271 -> 3. Parse fail pe None
    (caller position-fallback use karega).
    """
    if batch_year is None:
        return None
    match = re.fullmatch(r"(\d{2})(\d{2})(\d)", str(session_id).strip())
    if not match:
        return None
    start_yy, end_yy, term = (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
    )
    if end_yy != start_yy + 1 or term not in (1, 2):
        return None
    sem = (start_yy - batch_year) * 2 + term
    return sem if 1 <= sem <= 12 else None


def declared_result_semesters(exam_results):
    """Result page ke SGPA list me kaunse Sem N declare hain (set[int])."""
    declared = set()
    for res in exam_results or []:
        match = re.search(
            r"\b(?:semester|sem)\s*[-:]?\s*(\d+)\b",
            str((res or {}).get("semester", "")),
            flags=re.I,
        )
        if match:
            declared.add(int(match.group(1)))
    return declared


def numbered_sessions(raw_sessions, uid):
    """⭐ Sessions ko unke ASLI semester-numbers do (oldest-first).

    Har item: {"id", "name", "sem_num"}. id-digits parse pehle (UID ke
    batch-year se), fail pe position-fallback. Isse dropdown labels
    kabhi ult nahi hoti - 25262 hamesha "Semester 2" rahega, chahe pool
    me kitne bhi sessions hon.
    """
    batch_match = re.match(r"(\d{2})", str(uid or ""))
    batch_year = int(batch_match.group(1)) if batch_match else None
    ordered = sort_academic_sessions(raw_sessions)
    out = []
    used = set()
    for sess in ordered:
        sid = str(sess.get("id", ""))
        sem_num = academic_semester_number(sid, batch_year)
        if sem_num is not None and sem_num not in used:
            used.add(sem_num)
        else:
            sem_num = 0  # placeholder - fallback neeche fill hoga
        out.append({
            "id": sid,
            "name": str(sess.get("name", "")),
            "sem_num": sem_num,
        })
    next_free = 1
    for item in out:
        if item["sem_num"] == 0:
            while next_free in used:
                next_free += 1
            item["sem_num"] = next_free
            used.add(next_free)
    return out


def merge_session_pool(primary, extra):
    """⭐ Session option lists ka UNION (id ke hisaab se).

    Marks page ka dropdown aur result.aspx ka dropdown alag-alag session
    lists dete hain - result page me puraane semesters (Sem 2...) ho
    sakte hain jo marks page me nahi hote. Dono mila ke poora pool.

    ⭐ JUNK FILTER: result.aspx ke ddlResultType options (Final/Session)
    aur ddlCategory (REG/REP) session IDs NAHI hain. Ye marks page ke
    select se leak hokar pool ko pollute kar dete the, jisse dropdown
    me "Final"/"Session" dikhta aur uspe click karne se result postback
    galat dropdown (ResultType) pe chala jaata tha -> Sem 1/2 swap!
    """
    junk_ids = {
        "final", "session", "reg", "rep",
        "regular", "re-appear", "reappear",
    }
    pool = []
    seen = set()
    for item in (primary or []) + (extra or []):
        sid = str((item or {}).get("id", "")).strip()
        if not sid or sid in seen:
            continue
        if sid.lower() in junk_ids:
            continue
        seen.add(sid)
        pool.append({
            "id": sid,
            "name": str((item or {}).get("name", "")).strip(),
        })
    return pool


def normalize_subject_grades(subjects):
    grade_scores = {
        "O": 95, "A+": 88, "A": 82, "B+": 75,
        "B": 68, "C+": 58, "C": 50, "D": 40, "F": 25,
    }

    cleaned = []
    total_credits = 0
    seen_codes = set()

    for subject in subjects or []:
        code = str(subject.get("code", "")).strip()
        if not code or code.upper() in seen_codes:
            continue
        seen_codes.add(code.upper())

        title = str(
            subject.get("title") or subject.get("subject") or ""
        ).strip()
        grade = str(subject.get("grade", "B")).strip().upper()
        credits = subject.get("credits", "0")

        try:
            total_credits += int(float(str(credits)))
        except (TypeError, ValueError):
            pass

        # ⭐ Result page ke Internal/External marks (sessional-display
        # fallback) - template inhe tab dikhata hai jab marks page
        # khaali ho.
        internal = str(subject.get("internal", "") or "").strip()
        external = str(subject.get("external", "") or "").strip()

        # ⭐ Actual TOTAL = Internal + External (portal grade-sheet ka
        # total - e.g. 58.21+18.00 = 76.21 for Computer Eco-System).
        # Pehle grade se guess hota tha (B=68) jo portal marks se
        # MISMATCH lagta tha ("score wrong" complaint, live fix).
        # Dono marks blank hon tabhi grade se fallback.
        def _mnum(value):
            try:
                return float(str(value))
            except (TypeError, ValueError):
                return None

        i_num = _mnum(internal)
        e_num = _mnum(external)
        if i_num is not None or e_num is not None:
            score = round((i_num or 0.0) + (e_num or 0.0), 2)
            if float(score).is_integer():
                score = int(score)
        else:
            score = grade_scores.get(grade, 75)

        cleaned.append({
            "code": code,
            "subject": title,
            "short_subject": title[:11] + ".." if len(title) > 13 else title,
            "credits": credits,
            "grade": grade,
            "score": score,
            "internal": internal,
            "external": external,
        })

    return cleaned, total_credits


def save_exam_result_to_session(request, exam_result):
    """Always replace old semester data, including when the new list is empty."""
    if not exam_result or not exam_result.get("success"):
        request.session["exam_results"] = []
        request.session["subject_grades"] = []
        request.session["student_cgpa"] = "0.00"
        request.session["total_credits"] = 0
        request.session["result_pending"] = False
        return

    request.session["exam_results"] = exam_result.get("results", [])
    request.session["student_cgpa"] = exam_result.get("global_cgpa", "0.00")

    # ⭐ Ongoing semester (Sem 3) ka result portal pe declare nahi hua -
    # template pending-card dikhata hai (Sem 2 ke subjects Sem 3 ke
    # neeche confuse kar rahe the - backend ab unhe nahi bhejta).
    request.session["result_pending"] = bool(
        exam_result.get("semester_pending")
    )

    # ⭐ result.aspx ka apna semester dropdown (available_sems) - ye
    # marks page se ZYADA sessions rakh sakta hai (Sem 2 wala case).
    # Ise pool me merge karke results dropdown ko complete rakhte hain.
    request.session["result_available_sems"] = (
        exam_result.get("available_sems") or []
    )
    request.session["result_active_sem"] = str(
        exam_result.get("active_sem") or ""
    )

    # ⭐ Page ke ACTIVE semester ka SGPA/CGPA direct (postback ke baad ye
    # us semester ki values hain) - history-regex fail ho to bhi cards
    # sahi dikhenge.
    request.session["result_active_sgpa"] = str(
        exam_result.get("active_sgpa") or ""
    )
    request.session["result_active_cgpa"] = str(
        exam_result.get("active_cgpa") or ""
    )

    subjects, total_credits = normalize_subject_grades(
        exam_result.get("subject_grades", [])
    )
    request.session["subject_grades"] = subjects
    request.session["total_credits"] = total_credits


def normalize_fee_data(summary, records, receipts_map=None):
    """Normalise the fee summary and attach receipt download URLs.

    Expected shapes (same keys the scraper backend should return):
      summary: {"total": "...", "paid": "...", "due": "..."}
      records: [{"semester","title","amount","paid","due",
                 "date","status","receipt"}]

    ⭐ receipts_map: {receipt_no: {...}} - session me saved download
    links. Har record ko "receipt_url" milta hai jo hamara
    fee_receipt_view proxy-download URL hota hai.
    """
    def money(value):
        try:
            return float(str(value).replace(",", "").replace("₹", "").strip())
        except (TypeError, ValueError):
            return 0.0

    summary = summary or {}
    total = money(summary.get("total"))
    paid = money(summary.get("paid"))
    due = money(summary.get("due"))
    # ⭐ v5.9 FEE-SANITY: paid > total matlab scraped "total" galat/adhoora
    # page se aaya (e.g. installment/partial total - user report: portal
    # ka asli total 27,200 tha, scrape ne 15,000 pakad liya -> CLEAR +
    # 181.3% ka fake meter bana). Due mili ho to liability = paid + due;
    # warna status honestly UNKNOWN (paisa hero band, receipts card).
    if total and paid > total:
        if due:
            total = paid + due
        else:
            total = 0.0
    # ⭐ v5.10-realdue: DUE > TOTAL bhi inconsistent hai (galat
    # regex-total, e.g. LAST-PAYMENT amount ko fee-total samajh liya
    # tha - user hard fact: "15000 meri last payment hai")
    # -> liability = paid + due
    if total and due > total:
        total = paid + due
    # ⭐ v5.10-realdue: LAST PAYMENT (payable table ka TOTAL row,
    # user hard fact) - hero subline hint, sirf display ke liye
    last_pay = money(summary.get("last_pay"))
    if not due and total:
        due = max(0.0, total - paid)
    # ⭐ v5.8 FEE-TOTAL: portal ne sirf DUE bataya (total nahi mila,
    # e.g. payment page pe "Total Payable Amount") to liability
    # approx = paid + due - tabhi status/meter sahi dikhtay hain.
    if not total and due:
        total = paid + due
    has_money = bool(total)

    receipts_map = receipts_map or {}
    cleaned_records = []
    for record in list(records or []):
        item = dict(record)
        receipt_no = str(item.get("receipt") or "").strip()
        item["receipt_url"] = ""
        if receipt_no and receipt_no in receipts_map:
            try:
                item["receipt_url"] = reverse(
                    "scraper_app:fee_receipt",
                    args=[receipt_no],
                )
            except Exception:
                item["receipt_url"] = ""
        cleaned_records.append(item)

    cleaned_summary = {
        "total": f"{total:,.0f}",
        "paid": f"{paid:,.0f}",
        "due": f"{due:,.0f}",
        # ⭐ v5.9: meter kabhi 100% se upar NAHI (scrape mismatch me
        # 181.3% jaisa impossible number aa raha tha)
        "paid_pct": min(round(paid / total * 100, 1), 100.0) if total else 0,
        # ⭐ v5.15-scaleall: bar ka red hissa = DUE share (100-paid_pct
        # ki jagah exact due ratio - dono milke full bar banate hain)
        "due_pct": min(round(due / total * 100, 1), 100.0) if total else 0,
        # ⭐ v5.9: CLEAR ke teeno shart - total ho, due zero ho, AUR
        # poora paid bhi ho (mismatch case me fake CLEAR banned)
        "cleared": bool(total) and due <= 0 and paid >= total - 0.5,
        # ⭐ v5.8 FEE-STATUS-HONEST: money hero + CLEAR/DUE status SIRF
        # jab asli TOTAL ho. Receipts-only scrape (sirf paid pata) pe
        # fee status UNKNOWN hota hai - paisa dikhana matlab jhootha
        # "CLEAR + 100%" (user bug). Aise me receipts-overview card.
        "has_money": has_money,
        # ⭐ v5.9: status UNKNOWN ho to bhi receipts ka paid total HINT
        # ke roop me dikhao (honest, koi status/meter claim ke bina)
        "paid_hint": "" if has_money else (f"{paid:,.0f}" if paid else ""),
        # ⭐ v5.10-realdue: hero subline "LAST PAYMENT ₹X" jab paid
        # unknown ho (portal receipts pe amounts nahi hote)
        "last_pay": f"{last_pay:,.0f}",
        # ⭐ v5.15-scaleall: scale ab HAMESHA money hero pe (user ask:
        # due account pe bhi 0-100% scale chahiye). paid unknown -> white
        # fill 0%, red fill = full due share - subline "LAST PAYMENT"
        # context de deta hai, koi jhootha claim nahi.
        "meter": has_money,
        # ⭐ v5.14-clearscale: backend ne all-paid CONFIRM kiya (receipts
        # hain + due/demand ka koi suraag nahi) - tab receipts-overview
        # hero pe 100% WHITE "FULLY PAID" scale honestly dikhta hai.
        "all_paid": str(summary.get("all_paid") or "").strip() in ("1", "true", "True"),
        "receipt_count": len(cleaned_records),
        "latest": cleaned_records[0].get("date", "") if cleaned_records else "",
    }
    return cleaned_summary, cleaned_records


def login_view(request):
    # ⭐ AUTO-DASHBOARD ("jab tak logout na karoon, dubara mat maango"):
    # session ka login abhi bhi zinda hai to app open karte hi seedha
    # dashboard - UID / password / captcha form dikhta hi nahi.
    # Fresh form chahiye (manual debug) to: login URL pe ?fresh=1
    if (
        request.method == "GET"
        and request.GET.get("fresh") != "1"
        and request.session.get("student_uid")
        and request.session.get("scraper_state")
        and request.session.get("attendance_data")
    ):
        return redirect("scraper_app:dashboard")

    if request.method == "POST":
        base_url = request.POST.get("base_url", DEFAULT_BASE_URL).strip()
        uid = request.POST.get("uid", "").strip()

        if not uid:
            return render(request, "scraper_app/login.html", {
                "error": "Student ID / UID is required.",
                "base_url": base_url,
                "bg": LOGIN_BG_MEDIA,
            })

        scraper = CUIMSScraperBackend(base_url=base_url, uid=uid)
        result = scraper.execute_stage1()

        if not result.get("success"):
            return render(request, "scraper_app/login.html", {
                "error": result.get("error", "UID validation failed."),
                "base_url": base_url,
                "uid": uid,
                "bg": LOGIN_BG_MEDIA,
            })

        request.session["scraper_state"] = {
            "uid": uid,
            "base_url": base_url,
            "target_post_url": result.get("target_post_url"),
            "mode": result.get("mode"),
            "captcha_input_name": result.get("captcha_input_name"),
            "payload_stage2_template": scraper.payload_stage2_template,
            "cookies": requests.utils.dict_from_cookiejar(scraper.session.cookies),
        }

        captcha_base64 = None
        if result.get("has_captcha") and scraper.captcha_image_bytes:
            captcha_base64 = base64.b64encode(
                scraper.captcha_image_bytes
            ).decode("utf-8")

        return render(request, "scraper_app/enter_password.html", {
            "uid": uid,
            "base_url": base_url,
            "has_captcha": result.get("has_captcha", False),
            "captcha_base64": captcha_base64,
            "bg": LOGIN_BG_MEDIA,
        })

    return render(request, "scraper_app/login.html", {
        "base_url": DEFAULT_BASE_URL,
        "bg": LOGIN_BG_MEDIA,
    })


def authenticate_view(request):
    if request.method != "POST":
        return redirect("scraper_app:login")

    password = request.POST.get("password", "")
    captcha_code = request.POST.get("captcha_code", "").strip()
    state = request.session.get("scraper_state")

    if not state:
        return render(request, "scraper_app/login.html", {
            "error": "Session expired. Please re-enter your Student ID.",
            "bg": LOGIN_BG_MEDIA,
        })

    uid = state["uid"]
    base_url = state["base_url"]
    scraper = CUIMSScraperBackend(base_url=base_url, uid=uid)
    scraper.target_post_url = state["target_post_url"]
    scraper.mode = state["mode"]
    scraper.captcha_input_name = state.get("captcha_input_name")

    auth_result = scraper.execute_stage2(
        password=password,
        captcha_code=captcha_code,
        stage2_payload=dict(state.get("payload_stage2_template", {})),
        cached_cookies=state.get("cookies", {}),
    )

    if not auth_result.get("success"):
        refresh = CUIMSScraperBackend(base_url=base_url, uid=uid)
        refresh_result = refresh.execute_stage1()

        if not refresh_result.get("success"):
            return render(request, "scraper_app/login.html", {
                "error": auth_result.get("error", "Authentication failed."),
                "base_url": base_url,
                "uid": uid,
                "bg": LOGIN_BG_MEDIA,
            })

        request.session["scraper_state"] = {
            "uid": uid,
            "base_url": base_url,
            "target_post_url": refresh_result.get("target_post_url"),
            "mode": refresh_result.get("mode"),
            "captcha_input_name": refresh_result.get("captcha_input_name"),
            "payload_stage2_template": refresh.payload_stage2_template,
            "cookies": requests.utils.dict_from_cookiejar(refresh.session.cookies),
        }

        captcha_base64 = None
        if refresh_result.get("has_captcha") and refresh.captcha_image_bytes:
            captcha_base64 = base64.b64encode(
                refresh.captcha_image_bytes
            ).decode("utf-8")

        return render(request, "scraper_app/enter_password.html", {
            "error": auth_result.get("error", "Authentication failed."),
            "uid": uid,
            "base_url": base_url,
            "has_captcha": refresh_result.get("has_captcha", False),
            "captcha_base64": captcha_base64,
            "bg": LOGIN_BG_MEDIA,
        })

    cookies = auth_result.get("cookies", {})

    # Persist the authenticated cookie jar immediately. This prevents
    # semester-switch requests from using stale pre-login cookies.
    state["cookies"] = requests.utils.dict_from_cookiejar(
        scraper.session.cookies
    )
    request.session["scraper_state"] = state
    request.session.modified = True
    # ⭐ Known-good snapshot - restore point in case a later scraping
    # step (e.g. a wrong fee URL) makes UIMS reset the session.
    post_auth_cookies = dict(state.get("cookies", {}))

    attendance_result = scraper.scrape_attendance_records(cookies)

    if not attendance_result.get("success"):
        return render(request, "scraper_app/login.html", {
            "error": attendance_result.get("error", "Attendance scraping failed."),
            "base_url": base_url,
            "uid": uid,
            "bg": LOGIN_BG_MEDIA,
        })

    # ⭐ Shared cleaner - realtime sync (dashboard_data_view) bhi yahi
    # use karta hai, mapping har jagah same rahegi.
    cleaned_records = clean_attendance_records(attendance_result.get("records", []))

    # ⭐ v5.4 PORTAL OVERALL: scrape ne portal ka APNA overall % nikala
    # ho (page/JSON) to session me rakho - dashboard isi ko dikhayega
    # (user report: portal ka overall app ke weighted average se alag).
    # Debug line teeno candidates dikhati hai: portal / weighted /
    # avg-of-percents - console se confirm ho jayega portal kaunsa use
    # karta hai (agar None aaye to fallback weighted hi chalega).
    portal_overall = attendance_result.get("overall")
    try:
        _w_att = sum(int(r.get("attended", 0)) for r in cleaned_records)
        _w_held = sum(int(r.get("total", 0)) for r in cleaned_records)
        _weighted = round(_w_att / _w_held * 100, 1) if _w_held else 0
        _pa = [float(r.get("percentage", 0) or 0) for r in cleaned_records]
        _pact = [
            float(r.get("percentage", 0) or 0)
            for r in cleaned_records
            if int(r.get("total", 0) or 0) > 0
        ]
        _avg_all = round(sum(_pa) / len(_pa), 1) if _pa else 0
        _avg_act = round(sum(_pact) / len(_pact), 1) if _pact else 0
        print(
            f"[Attendance] overall compare: portal={portal_overall} "
            f"weighted={_weighted} avg-all={_avg_all} avg-active={_avg_act}"
        )
    except Exception:
        pass
    request.session["attendance_overall"] = portal_overall

    # ⭐ Debug: portal ke raw attendance record ke ASLI key names dump karo.
    # Agar attended/total 0 aaye to attendance_debug.txt se exact keys
    # dikh jayenge - wo file paste karo, candidate list me add kar dunga.
    raw_attendance = attendance_result.get("records", [])
    if raw_attendance:
        try:
            first_keys = sorted(str(k) for k in raw_attendance[0].keys())
            print(f"[Attendance] raw keys: {first_keys}")
            with open("attendance_debug.txt", "w", encoding="utf-8") as dbg:
                dbg.write(f"records={len(raw_attendance)}\n")
                dbg.write("KEYS: " + ", ".join(first_keys) + "\n\n")
                dbg.write("FIRST RECORD:\n")
                dbg.write(json.dumps(raw_attendance[0], indent=2)[:2000])
                dbg.write("\n\nALL RECORDS:\n")
                dbg.write(json.dumps(raw_attendance, indent=2)[:8000])
        except Exception:
            pass
        # Aggregate dikhe par attended sab 0 ho - keys match nahi hue warn karo
        if all(r["attended"] == 0 for r in cleaned_records) and any(
            r["percentage"] > 0 for r in cleaned_records
        ):
            print(
                "[Attendance] WARNING: percentage mila par attended/total 0 - "
                "attendance_debug.txt bhejo!"
            )

    # ⭐ "Jab tak logout na karoon" guarantee: 1 saal ka session, har
    # request pe renew (settings.SESSION_SAVE_EVERY_REQUEST=True ke saath
    # sliding) - app kholo to seedha dashboard; form sirf logout ke baad.
    request.session.set_expiry(60 * 60 * 24 * 365)
    request.session["student_uid"] = uid
    request.session["attendance_data"] = cleaned_records
    # ⭐ Day-wise detail pages ka TOKEN (bare GET pe ye portal 404 deti
    # hai) - raw attendance records ka EncryptCode session me rakho;
    # DailyAtt hunt mined/guess pages ke ?type= variants isi se banata hai.
    request.session["attendance_encrypt_codes"] = [
        str(record.get("EncryptCode")).strip()
        for record in (attendance_result.get("records") or [])
        if record.get("EncryptCode")
    ][:3]

    # ⭐⭐ v5.13 SPEED (login ab "der se" nahi khulta): pehle yahan 10+
    # scrapes SERIAL the - har portal round-trip ka wait agle ko block
    # karta tha (total = sabka sum). Ab phases me PARALLEL:
    #   Phase-1 safe pages ek saath (timetable/marks/fees/notices/
    #   profile/daily) - sab proven endpoints, session-kill risk nahi.
    #   Phase-2 exam results (marks codes + active session pe depend).
    #   Phase-3 risky guessed pages (hostel/course-plan) sabse END me -
    #   koi session kill ho bhi jaye to upar ka data save ho chuka hota
    #   hai; neeche health-probe post-login cookies restore kar deta hai.
    # Session writes sab main thread me hi hote hain (threads sirf
    # scrape karke result dete hain) - Django session race NAHI.
    _t0 = time.time()
    fee_scraper_fn = getattr(scraper, "scrape_fee_records", None)

    def _parallel(tasks, workers=6):
        out = {}
        with ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as pool:
            futures = {pool.submit(fn): name for name, fn in tasks}
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    out[name] = fut.result()
                except Exception as exc:
                    print(f"[Auth] parallel scrape '{name}' failed: {exc}")
                    out[name] = None
        return out

    _p1 = _parallel([
        ("timetable", lambda: scraper.scrape_timetable(cookies)),
        ("marks", lambda: scraper.scrape_marks_records(cookies)),
        ("fees", lambda: fee_scraper_fn(cookies) if callable(fee_scraper_fn) else None),
        ("notices", lambda: scraper.scrape_home_announcements(cookies)),
        ("profile", lambda: scraper.scrape_student_profile(cookies)),
        ("daily", lambda: scraper.scrape_daily_attendance(
            cookies,
            encrypt_codes=request.session.get("attendance_encrypt_codes"),
        )),
    ])
    timetable_result = _p1.get("timetable") or {}
    marks_result = _p1.get("marks") or {}
    fees_result = _p1.get("fees") or None
    notices_result = _p1.get("notices") or None
    profile_result = _p1.get("profile") or None
    daily_result = _p1.get("daily") or None
    print(f"[Auth] phase-1 parallel (6 pages) done in {time.time() - _t0:.1f}s")

    if timetable_result.get("success"):
        # ⭐ scrape ke time hi slots ko time ke anusar sort kar do
        request.session["timetable_data"] = normalize_timetable_map(
            timetable_result.get("timetable", {})
        )
    else:
        request.session["timetable_data"] = get_mock_timetable()

    if marks_result.get("success"):
        request.session["marks_data"] = marks_result.get("marks", [])
        request.session["available_sessions"] = marks_result.get(
            "available_sessions",
            []
        )
        request.session["active_session"] = marks_result.get(
            "active_session",
            ""
        )
    else:
        request.session["marks_data"] = []
        # ⭐ Marks fail pe session-pool MAT udao - warna RESULTS tab ka
        # semester dropdown hi gayab ho jaata hai (asli reported bug).
        # Pehle ka pool zinda rakho; neeche result.aspx ke apne sems
        # merge honge aur pool complete kar denge.
        request.session["available_sessions"] = (
            request.session.get("available_sessions") or []
        )
        request.session["active_session"] = (
            request.session.get("active_session") or ""
        )

    # ⭐ Fee records are OPTIONAL: scraper backend me scrape_fee_records
    # na ho to Fees tab bas empty render hota hai - login flow kabhi
    # nahi tootta.
    if fees_result and fees_result.get("success"):
        request.session["fee_summary"] = fees_result.get("summary", {})
        request.session["fee_records"] = fees_result.get("records", [])
        # ⭐ Receipt download links (receipt_no -> {page_url, link}).
        request.session["fee_receipts_map"] = fees_result.get("receipts_map", {})
    else:
        request.session["fee_summary"] = {}
        request.session["fee_records"] = []
        request.session["fee_receipts_map"] = {}

    # ⭐ Notices: StudentHome.aspx (CU UIMS home page) ke announcements.
    if notices_result and notices_result.get("success"):
        request.session["announcements"] = notices_result.get("announcements", [])
    else:
        request.session["announcements"] = []

    # Scrape the exam results for the SAME SESSION that is active on the
    # marks page. result.aspx ke dropdown me wahi SESSION IDs hote hain
    # jo marks page pe (chhote "1"/"2" nahi) - marks codes ke saath match
    # hote hi Sem 1/2 mix hone ka risk nahi.
    login_mark_codes = [
        item.get("code", "")
        for item in marks_result.get("marks", [])
        if item.get("code")
    ]

    _uid_txt = str(request.session.get("student_uid") or "")
    _batch_m = re.match(r"^(\d{2})", _uid_txt)
    _batch_year = int(_batch_m.group(1)) if _batch_m else None

    _t1 = time.time()
    exam_result = scraper.scrape_exam_results(
        cookies,
        sem_id=request.session.get("active_session") or None,
        marks_codes=login_mark_codes,
        # ⭐ Active session ka academic number bhi do - ongoing semester
        # active ho to backend SABSE PURAANI table na uthaye.
        semester_number=academic_semester_number(
            request.session.get("active_session"),
            _batch_year,
        ),
    )
    save_exam_result_to_session(request, exam_result)
    print(f"[Auth] phase-2 exam results done in {time.time() - _t1:.1f}s")

    # ⭐ Session pool = marks-page list + result-page list (union).
    request.session["available_sessions"] = merge_session_pool(
        request.session.get("available_sessions"),
        request.session.get("result_available_sems"),
    )
    if not request.session.get("active_session") and request.session.get(
        "result_active_sem"
    ):
        request.session["active_session"] = request.session[
            "result_active_sem"
        ]
    print(
        "[Sessions] marks_ok=%s pool=%s active=%s"
        % (
            marks_result.get("success"),
            [s.get("id") for s in request.session["available_sessions"]],
            request.session.get("active_session"),
        )
    )

    # Keep the authenticated session stable first. Campus pages are
    # disabled here because guessed portal endpoints can redirect to
    # Login.aspx and invalidate/replace the active session.
    request.session["portal_sections"] = {}

    # ⭐ Phase-3 RISKY pages (hostel menu-discovery + My Courses page):
    # guessed endpoints kabhi-kabhi session ko Login.aspx pe phenk dete
    # hain - isliye SAB se END me, dono ek saath parallel. Fail ho to
    # empty state; neeche health-probe cookies restore kar dega.
    _t2 = time.time()
    _p3 = _parallel([
        ("hostel", lambda: scraper.scrape_hostel_details(cookies)),
        ("course_plan", lambda: scraper.scrape_course_plan(cookies)),
    ], workers=2)
    hostel_result = _p3.get("hostel") or None
    course_plan_result = _p3.get("course_plan") or None
    print(f"[Auth] phase-3 risky pages done in {time.time() - _t2:.1f}s")

    if hostel_result and hostel_result.get("success"):
        request.session["hostel_details"] = hostel_result
    else:
        request.session["hostel_details"] = {"found": False}

    if course_plan_result and course_plan_result.get("success"):
        request.session["course_plan"] = course_plan_result
    else:
        request.session["course_plan"] = {"found": False}

    # ⭐ Profile + Daily-attendance results (phase-1 parallel me aaye,
    # session writes main thread me deterministic order me).
    if profile_result and profile_result.get("success"):
        request.session["student_profile"] = profile_result
    else:
        request.session["student_profile"] = {"found": False}

    if daily_result and daily_result.get("success"):
        request.session["daily_attendance"] = daily_result
    else:
        request.session["daily_attendance"] = {"found": False}
    print(f"[Auth] ALL scrapes total {time.time() - _t0:.1f}s (parallel)")

    # ⭐ Health check: agar beech ki kisi request ne portal session kill
    # kar diya (fee discovery pehle ye kar rahi thi), to final cookie jar
    # poisoned hai aur semester switching "session expired" degi. Ek probe
    # request se check karo; dead ho to post-login cookies wapas lagao.
    final_cookies = requests.utils.dict_from_cookiejar(scraper.session.cookies)
    try:
        probe = scraper.session.get(
            scraper.auth_url + "frmMyTimeTable.aspx",
            timeout=10,
            allow_redirects=True,
        )
        if "login" in probe.url.lower():
            print("[Auth] Portal session died mid-scrape; restoring post-login cookies.")
            final_cookies = post_auth_cookies
    except Exception:
        pass

    state["cookies"] = final_cookies
    request.session["scraper_state"] = state
    request.session["portal_alive"] = True
    request.session.modified = True

    # ⭐ Background keepalive: dashboard band hone ke baad bhi portal
    # session ko har ~10 min ping karke zinda rakhega.
    start_portal_keepalive(base_url, uid, final_cookies)

    return redirect("scraper_app:dashboard")


def logout_view(request):
    """⭐ Logout = asli 'session band karo' button.

    Keepalive ping band -> Django session FLUSH (saved attendance, portal
    cookies, uid - sab delete). Iske baad hi app agli baar khulne pe
    UID + password + captcha maangega. Bilkul user ki demand ke hisaab se:
    'jab tak main logout na karoon, tab tak seedha dashboard; logout ke
    baad hi form'.
    """
    state = request.session.get("scraper_state") or {}
    uid = state.get("uid") or request.session.get("student_uid")
    stop_portal_keepalive(state.get("base_url") or DEFAULT_BASE_URL, uid)
    request.session.flush()
    return redirect("scraper_app:login")


def attach_plan_urls(course_plan):
    """⭐ My Courses ke har course ko on-demand lecture-plan URL do.

    Lecture plan portal pe PDF format me hota hai - login pe download
    nahi karte (slow); click pe course_plan_pdf_view proxy official PDF
    serve karta hai (fee receipt wala hi pattern). Reverse fail ho
    (urls.py me route abhi add nahi hua) to URL khaali chhod dete hain -
    dashboard kabhi crash nahi karega.
    """
    plan = dict(course_plan or {"found": False})

    attached = []
    for index, course in enumerate(plan.get("courses") or []):
        item = dict(course)
        item["plan_view_url"] = ""
        if (
            item.get("plan_pdf")
            or item.get("plan_url")
            or item.get("postback")
            or item.get("plan_button")
        ):
            try:
                item["plan_view_url"] = reverse(
                    "scraper_app:course_plan_pdf", args=[index]
                )
            except Exception:
                item["plan_view_url"] = ""
        attached.append(item)
    plan["courses"] = attached

    page_pdfs = []
    for offset, entry in enumerate(plan.get("page_pdfs") or []):
        link = dict(entry)
        try:
            # ⭐ 1000+offset = page-level plan link (kisi course se
            # attach na hua) - view me wapas map hota hai.
            link["view_url"] = reverse(
                "scraper_app:course_plan_pdf", args=[1000 + offset]
            )
        except Exception:
            link["view_url"] = ""
        page_pdfs.append(link)
    plan["page_pdfs"] = page_pdfs
    return plan


def dashboard_view(request):
    uid = request.session.get("student_uid")
    records = request.session.get("attendance_data") or []

    if not records:
        return redirect("scraper_app:login")

    # ⭐ Server restart hua ho to keepalive thread wapas zinda karo -
    # baar-baar UID/password/captcha nahi bharna padega.
    ensure_keepalive_running(request)

    requested_session = request.GET.get("session_id")
    state = request.session.get("scraper_state")
    current_session = str(
        request.session.get("active_session") or ""
    )

    # ⭐ Academic numbering + semester VISIBILITY:
    #   - Har session ka ASLI Sem-N number (UID batch-year se) - labels
    #     kabhi ult nahi hoti (25262 hamesha "Semester 2")
    #   - ⭐ v5.17-showallsem: dropdown me SAARE semesters DIKHAO - Sem 3
    #     pending ho to bhi (user request: "Sem 3 dropdown me nahi dikh
    #     raha"). Tap karne pe us semester ke sessional marks + pending
    #     card milte hain; result declare hote hi v5.16 sync + sig auto-
    #     reload data bhar deta hai. (Purana rule ongoing ko HIDE karta
    #     tha - user ki pehle ki request pe.)
    #   - Dashboard ka DEFAULT view = newest COMPLETED semester hi rahega
    #     (completed_ids auto-switch neeche) - Sem 3 khud tap karne pe.
    numbered = numbered_sessions(
        merge_session_pool(
            request.session.get("available_sessions") or [],
            request.session.get("result_available_sems") or [],
        ),
        uid,
    )
    declared_sem_nums = declared_result_semesters(
        request.session.get("exam_results") or []
    )
    visible_pairs = list(numbered)
    # DEFAULT view ke liye sirf declared/completed semesters (pool me ho
    # sakta hai koi bhi - safe fallback poora list).
    completed_pairs = [
        sn for sn in numbered
        if sn["sem_num"] in declared_sem_nums
    ] or list(numbered)
    visible_ids = [sn["id"] for sn in visible_pairs]
    completed_ids = [sn["id"] for sn in completed_pairs]

    if (
        requested_session is None
        and current_session
        and current_session not in completed_ids
        and completed_ids
    ):
        # ⭐ Active session pending/undeclared hai (Sem 3) - dashboard
        # ka DEFAULT newest COMPLETED (Sem 2) kholo; user dropdown se
        # "Semester 3 · Pending" tap karke uska view le sakta hai.
        requested_session = completed_ids[-1]

    # Do not scrape again when the same semester page is rendered.
    # Repeated scraping was clearing the data after it was displayed once.
    should_switch_session = (
        requested_session is not None
        and str(requested_session) != current_session
    )

    if should_switch_session:
        requested_session = str(requested_session)

        if state:
            scraper = CUIMSScraperBackend(
                base_url=state["base_url"],
                uid=state["uid"],
            )
            cookies = state.get("cookies", {})

            # Fetch first. Do not clear the currently visible semester
            # until the new request succeeds; otherwise one failed
            # postback makes the page appear empty.
            marks_result = scraper.scrape_marks_records(
                cookies_dict=cookies,
                session_id=requested_session,
            )

            raw_sessions = request.session.get("available_sessions") or []

            # ⭐ Academic number (id-digits se) - position-hack se kabhi
            # Sem 1/2 swap nahi hota
            semester_number = next(
                (sn["sem_num"] for sn in numbered
                 if sn["id"] == requested_session),
                None,
            )

            selected_marks_codes = [
                item.get("code", "")
                for item in marks_result.get("marks", [])
                if item.get("code")
            ]

            exam_result = scraper.scrape_exam_results(
                cookies_dict=cookies,
                sem_id=requested_session,
                marks_codes=selected_marks_codes,
                # ⭐ Academic semester number (session-id digits se) -
                # backend isi se sahi per-semester grade table choose
                # karta hai (Sem 1/2 swap ka final fix).
                semester_number=semester_number,
            )

            print("========== SEMESTER DEBUG ==========")
            print("Requested session:", requested_session)
            print("Available sessions:", raw_sessions)
            print("Mapped semester:", semester_number)
            print("Marks success:", marks_result.get("success"))
            print("Marks error:", marks_result.get("error"))
            print("Marks codes:", [
                item.get("code")
                for item in marks_result.get("marks", [])
            ])
            print("Results success:", exam_result.get("success"))
            print("Results error:", exam_result.get("error"))
            print("Subject codes:", [
                item.get("code")
                for item in exam_result.get("subject_grades", [])
            ])
            print("====================================")

            # Commit marks only when the marks request succeeded.
            # A failed refresh must not erase the previous visible data.
            if marks_result.get("success"):
                request.session["marks_data"] = marks_result.get(
                    "marks",
                    [],
                )

            # Commit results only when the result request succeeded.
            if exam_result.get("success"):
                save_exam_result_to_session(
                    request,
                    exam_result,
                )
                # ⭐ Switch ke baad bhi result.aspx ke sems pool me
                # merge rakho (dropdown hamesha complete rahe).
                request.session["available_sessions"] = merge_session_pool(
                    request.session.get("available_sessions"),
                    request.session.get("result_available_sems"),
                )

            # Mark the new session active only if at least one fresh
            # portal request succeeded. Failed requests can be retried.
            if (
                marks_result.get("success")
                or exam_result.get("success")
            ):
                request.session["active_session"] = requested_session

            state["cookies"] = requests.utils.dict_from_cookiejar(
                scraper.session.cookies
            )
            request.session["scraper_state"] = state
            request.session.modified = True

        else:
            request.session["active_session"] = requested_session

    active_tab = request.GET.get("tab", "attendance")

    # ⭐ Profile LAZY-FETCH: student_profile session me nahi hai (ye feature
    # baad me aaya tha / login root-app jaisi kisi aur jagah se hua tha) to
    # yahi dashboard load pe EK BAAR scrape karke cache kar do - warna
    # PROFILE tab hamesha khaali dikhta. Console ka [Profile] line ab
    # dashboard refresh pe bhi dikhega, sirf login pe nahi. Authenticate
    # waise bhi key set karta hi hai, to fresh login ke baad ye block
    # practically kabhi chalta nahi - sirf legacy/root-login sessions ke liye.
    # NOTE: ?refresh_profile=1 se re-scrape hota hai (PROFILE tab ka
    # "Sync" chip) - parser update ke baad pura logout karke re-login
    # karne ki zaroorat nahi.
    refresh_profile = request.GET.get("refresh_profile") == "1"
    if refresh_profile or "student_profile" not in request.session:
        state = request.session.get("scraper_state") or {}
        cookies = state.get("cookies") or {}
        if cookies:
            try:
                scraper = CUIMSScraperBackend(
                    base_url=state.get("base_url") or DEFAULT_BASE_URL,
                    uid=state.get("uid", ""),
                )
                profile_result = scraper.scrape_student_profile(cookies)
                request.session["student_profile"] = (
                    profile_result
                    if profile_result and profile_result.get("success")
                    else {"found": False}
                )
            except Exception:
                request.session["student_profile"] = {"found": False}

    # ⭐ Day-wise attendance LAZY-FETCH (profile wala hi pattern -
    # legacy/root-login sessions me bhi DAY LOG panel khaali na rahe,
    # aur re-login ki zaroorat na pade). ?refresh_daily=1 = ATTENDANCE
    # ke Day-wise Log header ka ↻ Sync chip.
    refresh_daily = request.GET.get("refresh_daily") == "1"
    if refresh_daily or "daily_attendance" not in request.session:
        state = request.session.get("scraper_state") or {}
        cookies = state.get("cookies") or {}
        if cookies:
            try:
                daily_scraper = CUIMSScraperBackend(
                    base_url=state.get("base_url") or DEFAULT_BASE_URL,
                    uid=state.get("uid", ""),
                )
                daily_result = daily_scraper.scrape_daily_attendance(
                    cookies,
                    encrypt_codes=request.session.get("attendance_encrypt_codes"),
                )
                request.session["daily_attendance"] = (
                    daily_result
                    if daily_result and daily_result.get("success")
                    else {"found": False}
                )
            except Exception:
                request.session["daily_attendance"] = {"found": False}

    timetable = request.session.get("timetable_data") or get_mock_timetable()
    # ⭐ render ke waqt bhi sort karo, taaki purani (unsorted) session
    #    data ke saath bina re-login ke bhi schedule sorted dikhe
    timetable = sort_timetable_slots(timetable)
    marks = request.session.get("marks_data") or []
    # ⭐ Display-time merge: marks-page pool + result.aspx ke sems.
    # Purane (pre-fix) logins ka bhi dropdown complete dikhe, bina
    # re-login forced kiye.
    raw_sessions = merge_session_pool(
        request.session.get("available_sessions") or [],
        request.session.get("result_available_sems") or [],
    )
    active_session = str(request.session.get("active_session") or "")
    exam_results = request.session.get("exam_results") or []
    subject_grades = request.session.get("subject_grades") or []
    student_cgpa = request.session.get("student_cgpa") or "0.00"
    total_credits = request.session.get("total_credits") or 0
    portal_sections = request.session.get("portal_sections") or {}

    # ⭐ Fees tab ka data (session se, normalise karke) - har receipt ke
    # saath proxy download URL attach hota hai.
    fee_summary, fee_records = normalize_fee_data(
        request.session.get("fee_summary"),
        request.session.get("fee_records"),
        request.session.get("fee_receipts_map"),
    )

    # ⭐ v5.17: dropdown = SAARE semesters; ongoing/pending pe tag.
    # labels = academic Sem-N numbers (25262 hamesha "Semester 2")
    available_sessions = [
        {
            "id": sn["id"],
            "name": f"Semester {sn['sem_num']}",
            "selected": sn["id"] == active_session,
            # ⭐ Result abhi declare nahi hua -> option me "· Pending"
            "pending": sn["sem_num"] not in declared_sem_nums,
        }
        for sn in visible_pairs
    ]

    # Do not filter result subjects by sessional-mark codes.
    subject_grades, calculated_credits = normalize_subject_grades(
        subject_grades
    )
    if calculated_credits:
        total_credits = calculated_credits

    active_sgpa = "0.00"

    # ⭐ Academic number se SGPA match (position-hack ka mix-up khatam)
    active_semester_number = next(
        (sn["sem_num"] for sn in numbered if sn["id"] == active_session),
        None,
    )

    # NOTE: The result page is now parsed per-semester by the backend
    # (scrape_exam_results selects the correct per-semester grade table via
    # subject-code overlap and drops the combined aggregate table), so
    # subject_grades already contains ONLY the active semester's subjects.
    # No hard-coded code whitelist or marks-code re-filtering is needed
    # here - doing so previously dropped valid subjects down to a single
    # row. We only normalise/dedupe and recompute credits.

    if active_semester_number is not None:
        for result in exam_results:
            result_text = str(result.get("semester", ""))
            match = re.search(
                r"\b(?:semester|sem)\s*[-:]?\s*(\d+)\b",
                result_text,
                flags=re.I,
            )
            if match and int(match.group(1)) == active_semester_number:
                active_sgpa = result.get("sgpa", "0.00")
                break

    # ⭐ History khaali/parse fail ho to page ke ACTIVE semester ki
    # direct values (backend ne result page ke card se uthai hain)
    if active_sgpa == "0.00" and request.session.get("result_active_sgpa"):
        active_sgpa = request.session["result_active_sgpa"]

    if active_sgpa == "0.00" and exam_results:
        active_sgpa = exam_results[-1].get("sgpa", "0.00")

    # ⭐ CGPA bhi: global parse nahi hua to active semester wala direct
    if (not student_cgpa or student_cgpa == "0.00") and request.session.get(
        "result_active_cgpa"
    ):
        student_cgpa = request.session["result_active_cgpa"]

    # ⭐ v5.1 COURSE MODAL data-pack: course card click pe full modal
    # khulta hai (user screenshot: Prediction / Timeline / Course Plan
    # tabs + % ring + recovery pill). Har attendance record ko enrich:
    #   dlog        - subject P/A summary (p/a/pct + flat entries)
    #   dlog_days   - Timeline tab: day-grouped entries (rail + headers)
    #   dproj       - Prediction tab: "+k classes attend -> X%" rows
    #   ring_off    - header SVG % ring ka stroke-dashoffset
    #   dplan_url / dplan_credits - Course Plan tab (proxy PDF + credits)
    try:
        _dsubj = (
            request.session.get("daily_attendance") or {}
        ).get("subjects") or []
        _smap = {}
        for _s in _dsubj:
            if not isinstance(_s, dict):
                continue
            _k = str(_s.get("code") or "").strip().upper()
            if _k:
                _smap[_k] = _s

        # Course Plan tab: subject code -> official lecture-plan PDF
        # proxy URL (course_plan_pdf_view) + meta se credits chip.
        _pmap = {}
        try:
            _cplan = attach_plan_urls(
                request.session.get("course_plan")
            ).get("courses") or []
        except Exception:
            _cplan = []
        for _pc in _cplan:
            if not isinstance(_pc, dict):
                continue
            _pk = str(_pc.get("code") or "").strip().upper()
            if not _pk:
                continue
            _credits = ""
            for _m in _pc.get("meta") or []:
                _cm = re.search(
                    r"(\d+(?:\.\d+)?)\s*Credits?", str(_m), re.I
                )
                if _cm:
                    _credits = f"{_cm.group(1)} Credits"
                    break
            _pmap[_pk] = (
                str(_pc.get("plan_view_url") or ""),
                _credits,
            )

        def _days_of(entries):
            # flat entries -> day groups (rail + day-header data)
            groups = []
            cur = None
            for e in entries:
                if cur is None or cur["date"] != e.get("date"):
                    cur = {
                        "date": e.get("date", ""),
                        "wday": e.get("wday", ""),
                        "p": 0,
                        "a": 0,
                        "entries": [],
                    }
                    groups.append(cur)
                cur["entries"].append(e)
                if e.get("tone") == "present":
                    cur["p"] += 1
                elif e.get("tone") == "absent":
                    cur["a"] += 1
            return groups

        _packed = []
        for r in records:
            _rk = str(r.get("code") or "").strip().upper()
            _subj = _smap.get(_rk)
            _att = int(r.get("attended") or 0)
            _tot = int(r.get("total") or 0)
            try:
                _pct = float(r.get("percentage") or 0)
            except (TypeError, ValueError):
                _pct = 0.0
            _pu, _cr = _pmap.get(_rk, ("", ""))
            _packed.append(dict(
                r,
                dlog=_subj,
                dlog_days=(
                    _days_of(_subj.get("entries") or []) if _subj else []
                ),
                dproj=(
                    [
                        {
                            "k": k,
                            "pct": round((_att + k) * 100 / (_tot + k), 1),
                        }
                        for k in (1, 2, 3, 4, 5)
                    ]
                    if _tot else []
                ),
                ring_off=round(
                    188.5 * (100.0 - min(_pct, 100.0)) / 100.0, 1
                ),
                dplan_url=_pu,
                dplan_credits=_cr,
            ))
        records = _packed
        _linked = sum(1 for r in records if r.get("dlog"))
        _nplans = sum(1 for r in records if r.get("dplan_url"))
        print(
            f"[DailyAtt] course-modal pack: subjects={len(_smap)} "
            f"linked={_linked} plans={_nplans}"
        )
    except Exception as _dlog_exc:
        print(f"[DailyAtt] course-modal pack skip: {_dlog_exc}")

    total_attended = sum(int(r.get("attended", 0)) for r in records)
    total_held = sum(int(r.get("total", 0)) for r in records)
    # ⭐ v5.5 AVG DISPLAY (user request + portal proof 57.08%): overall %
    # ki jagah portal-jaisa AVG of course percentages dikhao:
    #   avg = mean(har course ka percentage)
    # Portal ne scrape me apna overall diya ho to wahi (authoritative),
    # warna avg-of-pcts. Weighted average sirf safety/bunk math ke liye.
    weighted_percentage = round(
        total_attended / total_held * 100,
        1,
    ) if total_held else 0.0
    # ⭐ v5.6 AVG-ACTIVE fix: average SIRF un courses ka jinme koi class
    # DELIVERED hui hai (total > 0). 0-delivered course (e.g. 25CST-208 ka
    # "No Data" 0/0) portal average se bahar rakhta hai - app me 0% leke
    # aa raha tha isliye 57.08 ki jagah 50.7 dikh rha tha.
    _pcts = [
        float(r.get("percentage", 0) or 0)
        for r in records
        if int(r.get("total", 0) or 0) > 0
    ]
    avg_percentage = round(
        sum(_pcts) / len(_pcts), 1,
    ) if _pcts else 0.0
    _portal_overall = request.session.get("attendance_overall")
    if isinstance(_portal_overall, (int, float)) and 0 <= float(_portal_overall) <= 100:
        global_percentage = round(float(_portal_overall), 1)
    else:
        global_percentage = avg_percentage

    if global_percentage >= 75:
        safe_bunks = max(
            0,
            int((100 * total_attended - 75 * total_held) // 75),
        )
        bunk_status = (
            f"Safe! You can bunk the next {safe_bunks} classes."
            if safe_bunks > 0
            else "Safe! You are exactly at 75%."
        )
    else:
        required_classes = max(
            0,
            int(((75 * total_held - 100 * total_attended) + 24) // 25),
        )
        bunk_status = (
            f"Critical: Attend {required_classes} classes "
            "consecutively to reach 75%."
        )

    # ⭐ RENDER-DEBUG (temporary): server khud PROOF deta hai ki kaunsa
    # physical template file render ho rahi hai AUR session se screen
    # tak kya values ja rahi hain. Refresh pe console me ye 2 lines
    # paste karo - screen vs server ka difference turant pakda jayega.
    try:
        from django.template.loader import get_template
        _tpl = get_template("scraper_app/dashboard.html")
        print(f"[Render] template={_tpl.origin.name}")
    except Exception as _tpl_exc:
        print(f"[Render] template check failed: {_tpl_exc}")
    if subject_grades:
        _s0 = subject_grades[0]
        print(
            f"[Render] sub0={_s0.get('code')} int={_s0.get('internal')!r} "
            f"ext={_s0.get('external')!r} score={_s0.get('score')!r} "
            f"marks={len(marks)} subs={len(subject_grades)}"
        )
    else:
        print(
            f"[Render] subject_grades EMPTY pending="
            f"{bool(request.session.get('result_pending'))}"
        )

    return render(request, "scraper_app/dashboard.html", {
        "uid": uid,
        "records": records,
        "id_card": bool(request.session.get("id_card")),
        "id_card_v": request.session.get("id_card_v") or 1,
        "timetable": timetable,
        "marks": marks,
        "available_sessions": available_sessions,
        "active_session": active_session,
        "exam_results": exam_results,
        "subject_grades": subject_grades,
        # ⭐ Ongoing semester ka result pending - template pending-card
        "result_pending": bool(request.session.get("result_pending")),
        "student_cgpa": student_cgpa,
        "total_credits": total_credits,
        "total_attended": total_attended,
        "total_held": total_held,
        # ⭐ Predict page (v6.7): missed chip (template me sub filter
        # nahi hota isliye views se computed jaata hai)
        "total_missed": total_held - total_attended,
        "global_percentage": global_percentage,
        "bunk_status": bunk_status,
        "active_tab": active_tab,
        "active_sgpa": active_sgpa,
        "portal_sections": portal_sections,
        "fee_summary": fee_summary,
        "fee_records": fee_records,
        # ⭐ Notices tab ka data (StudentHome announcements)
        "announcements": request.session.get("announcements") or [],
        # ⭐ Hostel tab ka data (StudentHome hostel-link discovery)
        "hostel": request.session.get("hostel_details") or {"found": False},
        # ⭐ Profile tab ka data (frmStudentProfile.aspx) + header avatar
        # ke liye portal ka asli naam (mile to; warna template fallback)
        "student_profile": request.session.get("student_profile") or {"found": False},
        # ⭐ Day-wise attendance log (kis din P/A) - ATTENDANCE panel ke neeche
        "daily_attendance": request.session.get("daily_attendance") or {"found": False},
        "display_name": (request.session.get("student_profile") or {}).get("name"),
        # ⭐ My Courses tab (frmMyCourse.aspx) - lecture plan ab official
        # PDF ke roop me khulta hai; proxy view URLs yahi attach hote hain
        "course_plan": attach_plan_urls(request.session.get("course_plan")),
        # ⭐ Realtime sync: page-load ka data signature (JS poll compare karta hai)
        "dashboard_sig": build_dashboard_sig(request),
    })


def fee_receipt_view(request, receipt_id):
    """⭐ Fee receipt ka official PDF proxy-download karo.

    Portal receipt PDF sirf authenticated portal-session se milta hai -
    browser ke paas wo session nahi hai, isliye server-side stored
    cookies + saved download link se PDF laake user ko bhejte hain.
    Amount kabhi parse nahi karte - WYSIWYG official receipt.
    """
    receipts_map = request.session.get("fee_receipts_map") or {}
    state = request.session.get("scraper_state")
    info = receipts_map.get(receipt_id)

    if not info or not state:
        return HttpResponseNotFound(
            "Ye receipt is session me nahi mili. Dobara login karke try karo."
        )

    scraper = CUIMSScraperBackend(
        base_url=state.get("base_url"),
        uid=state.get("uid"),
    )
    document = scraper.fetch_receipt_document(
        cookies_dict=state.get("cookies", {}),
        page_url=info.get("page_url"),
        link=info.get("link"),
    )

    if not document:
        return HttpResponse(
            "Portal session expire ho gaya - receipt ke liye dobara login karo.",
            status=503,
        )

    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", str(receipt_id)) or "receipt"
    response = HttpResponse(
        document["content"],
        content_type=document.get("content_type") or "application/pdf",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="{filename}.pdf"'
    )
    return response


def profile_photo_view(request):
    """⭐ Profile photo proxy.

    Portal ki <img> direct page me nahi chalti (portal cookies is app ke
    domain pe nahi hongi), isliye server-side fetch karke inline serve
    karte hain - bilkul fee-receipt aur lecture-plan PDF proxies jaisa.
    """
    profile = request.session.get("student_profile") or {}

    # ⭐ FIX 4: UIMS ki asli profile photo EMBEDDED base64 data-URI hoti
    # hai (scrape ke waqt hi imgFullProfilePic se pakad ke session me store
    # ho jaati hai). Wo ho to seedha decode karke serve karo - portal hit
    # ki zaroorat hi nahi, aur loader.gif proxy karne ka zamana gaya.
    photo_b64 = profile.get("photo_b64") or ""
    if photo_b64:
        raw_photo = b""
        try:
            raw_photo = base64.b64decode(photo_b64)
        except Exception:
            raw_photo = b""
        if len(raw_photo) > 300:
            photo_resp = HttpResponse(
                raw_photo,
                content_type=profile.get("photo_mime") or "image/jpeg",
            )
            # 5 min browser cache - dashboard render pe baar-baar work na ho
            photo_resp["Cache-Control"] = "private, max-age=300"
            print(f"[Profile] photo served (embedded) - {len(raw_photo)} bytes")
            return photo_resp

    state = request.session.get("scraper_state") or {}
    photo_url = profile.get("photo_url") or ""
    cookies = state.get("cookies") or {}
    if not (photo_url and cookies):
        return HttpResponseNotFound("Profile photo available nahi hai.")
    try:
        scraper = CUIMSScraperBackend(
            base_url=state.get("base_url", DEFAULT_BASE_URL),
            uid=state.get("uid", ""),
        )
        doc = scraper.fetch_profile_photo(cookies, photo_url)
    except Exception:
        doc = {"ok": False}
    if not doc.get("ok"):
        return HttpResponseNotFound("Profile photo abhi load nahi hua.")
    resp = HttpResponse(
        doc["content"],
        content_type=doc.get("content_type") or "image/jpeg",
    )
    # ⭐ 5 min browser cache - dashboard render pe baar-baar portal hit na ho
    resp["Cache-Control"] = "private, max-age=300"
    return resp


def course_plan_pdf_view(request, course_index):
    """⭐ My Courses ka lecture plan PDF proxy-open karo.

    Plan sirf authenticated portal-session se milta hai - browser ke
    paas wo session nahi hai, isliye server-side stored cookies + login
    ke waqt saved refs (pdf href / aspx href / __doPostBack target) se
    plan laake user ko bhejte hain (fee receipt wala hi pattern). PDF
    kabhi parse nahi karte - WYSIWYG official lecture plan.

    1000+index = page-level plan link (jo kisi course se attach na hua
    ho - panel ke top pe "Lecture Plan PDFs" list me dikhta hai).
    """
    course_plan = request.session.get("course_plan") or {}
    state = request.session.get("scraper_state")
    courses = course_plan.get("courses") or []

    if not state:
        return HttpResponseNotFound(
            "Session khatam ho gaya - dobara login karke try karo."
        )

    entry = None
    label = "lecture_plan"
    if 0 <= course_index < len(courses):
        entry = courses[course_index]
        label = entry.get("code") or label
    elif (
        1000 <= course_index
        < 1000 + len(course_plan.get("page_pdfs") or [])
    ):
        entry = course_plan["page_pdfs"][course_index - 1000]
        label = entry.get("label") or label

    if not entry:
        return HttpResponseNotFound(
            "Ye course/plan is session me nahi mila. Dobara login karke try karo."
        )

    scraper = CUIMSScraperBackend(
        base_url=state.get("base_url"),
        uid=state.get("uid"),
    )
    fetch_kwargs = {
        "cookies_dict": state.get("cookies", {}),
        "page_url": course_plan.get("source_url"),
        "plan_pdf": entry.get("plan_pdf") or entry.get("url"),
        "plan_url": entry.get("plan_url"),
        "postback": entry.get("postback"),
        "plan_button": entry.get("plan_button"),
    }
    # ⭐ Stale-file guard: purana scraper_backend.py (plan_button ke
    # bina) deploy ho gaya ho to 500 mat do - jo kwargs backend support
    # karta hai sirf wahi bhejo. Newest backend copy karna phir bhi
    # zaroori hai (server start pe [Backend] v3.1 line check karo).
    try:
        supported = set(inspect.signature(
            scraper.fetch_course_plan_document
        ).parameters)
        fetch_kwargs = {
            key: value for key, value in fetch_kwargs.items()
            if key in supported
        }
    except (TypeError, ValueError):
        fetch_kwargs.pop("plan_button", None)
    document = scraper.fetch_course_plan_document(**fetch_kwargs)
    print(f"[Plan] {label} -> {document['kind'] if document else 'MISS'}")

    if not document:
        return HttpResponse(
            "Lecture plan abhi nahi mila - ya to portal session expire ho "
            "gaya (dobara login karo), ya faculty ne plan upload nahi kiya.",
            status=503,
        )

    if document["kind"] == "pdf":
        filename = re.sub(r"[^A-Za-z0-9._-]+", "_", str(label)) or "lecture_plan"
        response = HttpResponse(
            document["content"],
            content_type="application/pdf",
        )
        # ⭐ inline = browser/mobile ke PDF viewer me seedha khule,
        # download file nahi banti (fee receipt attachment hota hai,
        # lecture plan bas dekhna hota hai).
        response["Content-Disposition"] = (
            f'inline; filename="{filename}_lecture_plan.pdf"'
        )
        return response

    # HTML fallback: plan tables ko simple dark page me dikha do.
    rows_html = []
    for section in document.get("sections") or []:
        for row in section.get("rows") or []:
            cells = "".join(
                "<td style='padding:8px 10px;border-bottom:1px solid #26262b;"
                f"font-size:12px'>{html_escape(str(cell)[:120])}</td>"
                for cell in row
            )
            rows_html.append(f"<tr>{cells}</tr>")
    html_page = (
        "<!doctype html><html><head><meta name='viewport' "
        "content='width=device-width, initial-scale=1'>"
        f"<title>Lecture Plan - {html_escape(str(label))}</title></head>"
        "<body style='background:#0b0b0d;color:#eee;font-family:system-ui,"
        "sans-serif;padding:20px;margin:0'>"
        f"<p style='color:#EC1C24;font-size:11px;letter-spacing:2px;"
        f"font-weight:700'>LECTURE PLAN &middot; {html_escape(str(label))}</p>"
        f"<table style='border-collapse:collapse;width:100%'>"
        f"{''.join(rows_html)}</table>"
        "<p style='color:#777;font-size:10px;margin-top:14px;letter-spacing:1px'>"
        "CUNNECT &middot; CU-ERP SYNC</p></body></html>"
    )
    return HttpResponse(html_page)


def dashboard_data_view(request):
    """⭐ REALTIME AUTO-SYNC endpoint (JSON) - dashboard page isko har
    60 sec me poll karta hai.

    Kya hota hai:
      - Har ~3 min me portal se timetable + notices + attendance dobara
        scrape (ye 3 endpoints login-flow se proven-safe hain - koi
        guessed URL nahi, to session kill ka risk nahi)
      - Session ka data update karo + cookie jar refresh
      - Response: attendance numbers (JS in-place update karta hai,
        bina reload) + data SIG (timetable/notices badle to JS ek baar
        auto-reload karta hai)
    """
    if not request.session.get("student_uid"):
        return JsonResponse({"ok": False, "stale": True}, status=401)

    # ⭐ Restart-recovery: keepalive thread dobara start (60-sec poll
    # isko har baar check karti hai - restart ke baad pehli poll pe hi).
    ensure_keepalive_running(request)

    now = time.time()
    last_sync = float(request.session.get("last_data_sync") or 0)
    state = request.session.get("scraper_state")
    did_sync = False
    base_url = (state or {}).get("base_url")
    uid = (state or {}).get("uid")

    if (
        state
        and base_url
        and (now - last_sync) >= DASHBOARD_RESCRAPE_MIN_SECONDS
    ):
        try:
            scraper = CUIMSScraperBackend(
                base_url=base_url,
                uid=uid,
            )
            # ⭐ Cookies keepalive-store se lo (background thread har
            # ~10 min rotate karta rehta hai); store khaali ho to
            # session wali fallback.
            ka_entry = get_keepalive_state(base_url, uid)
            cookies = ka_entry.get("cookies") or state.get("cookies", {})

            # 1) Timetable (frmMyTimeTable - health-probe wali safe page)
            tt_result = scraper.scrape_timetable(cookies)
            if tt_result.get("success"):
                request.session["timetable_data"] = normalize_timetable_map(
                    tt_result.get("timetable", {})
                )

            # 2) Notices (AJAX PageMethod - safe)
            nt_result = scraper.scrape_home_announcements(cookies)
            if nt_result.get("success"):
                request.session["announcements"] = nt_result.get("announcements", [])

            # 3) Attendance (GetReport - safe)
            at_result = scraper.scrape_attendance_records(cookies)
            if at_result.get("success"):
                # ⭐ v5.2 REALTIME DAILY: summary numbers BADLE (teacher ne
                # portal pe attendance chadhayi) to day-wise P/A log bhi
                # abhi rescrape karo - warna course modal ki Timeline
                # purani rehti agle relogin/↻Sync tak. Har sync pe NAHI
                # (9 extra portal-calls waste hote) - sirf change detect
                # pe. Sig me daily stats shamil hain to fresh data aate
                # hi page 1 baar auto-reload hota hai -> modal updated.
                _new_records = clean_attendance_records(
                    at_result.get("records", [])
                )
                _old_finger = [
                    (
                        str(r.get("code") or "").strip().upper(),
                        int(r.get("attended", 0)),
                        int(r.get("total", 0)),
                    )
                    for r in (request.session.get("attendance_data") or [])
                ]
                _new_finger = [
                    (
                        str(r.get("code") or "").strip().upper(),
                        int(r.get("attended", 0)),
                        int(r.get("total", 0)),
                    )
                    for r in _new_records
                ]
                request.session["attendance_data"] = _new_records
                # ⭐ v5.4: realtime rescrape pe portal overall bhi refresh
                request.session["attendance_overall"] = at_result.get("overall")
                # ⭐ EncryptCode tokens bhi refresh (day-wise detail pages
                # ke ?type= variants inhi se bante hain)
                request.session["attendance_encrypt_codes"] = [
                    str(r.get("EncryptCode")).strip()
                    for r in (at_result.get("records") or [])
                    if r.get("EncryptCode")
                ][:3]
                if (
                    request.session.get("daily_attendance", {}).get("found")
                    and _new_finger != _old_finger
                ):
                    print(
                        "[Sync] attendance numbers badle - day-wise P/A "
                        "log bhi refresh (modal Timeline realtime)"
                    )
                    try:
                        daily_result = scraper.scrape_daily_attendance(
                            cookies,
                            encrypt_codes=request.session.get(
                                "attendance_encrypt_codes"
                            ),
                        )
                        if daily_result and daily_result.get("success"):
                            request.session["daily_attendance"] = daily_result
                            print(
                                f"[Sync] day-wise refresh ok - records="
                                f"{daily_result.get('records', 0)} subjects="
                                f"{len(daily_result.get('subjects') or [])}"
                            )
                    except Exception as _de:
                        print(f"[Sync] day-wise refresh fail (old kept): {_de}")

            # 4) ⭐ v5.16-resultsync: koi semester result PENDING hai
            # (portal pe abhi declare nahi - e.g. Sem 3) to har sync pe
            # marks-view ka LIGHT check (login-safe proven page, koi
            # postback nahi). Subject codes BADLE/naye aaye = result
            # declare ho gaya -> full exam-results scrape + session
            # refresh (+ sig bump se page 1 baar auto-reload) = SEM 3
            # BINA RELOGIN app pe auto-show. Pending NAHI hai to ek bhi
            # extra portal call nahi hoti (zero cost gate).
            if request.session.get("result_pending"):
                try:
                    mk_result = scraper.scrape_marks_records(cookies)
                    _new_codes = sorted(
                        str(m.get("code") or "").strip().upper()
                        for m in (mk_result.get("marks") or [])
                        if m.get("code")
                    )
                    _old_codes = sorted(
                        str(m.get("code") or "").strip().upper()
                        for m in (request.session.get("marks_data") or [])
                        if m.get("code")
                    )
                    if (
                        mk_result.get("success")
                        and _new_codes
                        and _new_codes != _old_codes
                    ):
                        _uid_txt = str(
                            request.session.get("student_uid") or ""
                        )
                        _bm = re.match(r"^(\d{2})", _uid_txt)
                        _ex = scraper.scrape_exam_results(
                            cookies_dict=cookies,
                            sem_id=request.session.get("active_session")
                            or None,
                            marks_codes=_new_codes,
                            semester_number=academic_semester_number(
                                request.session.get("active_session"),
                                int(_bm.group(1)) if _bm else None,
                            ),
                        )
                        save_exam_result_to_session(request, _ex)
                        request.session["marks_data"] = mk_result.get(
                            "marks", []
                        )
                        request.session.modified = True
                        print(
                            f"[Sync] NEW RESULT DECLARED - marks="
                            f"{len(_new_codes)} pending="
                            f"{bool((_ex or {}).get('semester_pending'))} "
                            "- session refreshed (page auto-reload hoga, "
                            "Sem 3 ab dikhega)"
                        )
                except Exception as _rse:
                    print(
                        f"[Sync] result pending-check fail (old kept): "
                        f"{_rse}"
                    )

            # ⭐ LIVENESS CHECK: ek bhi scrape success nahi hua? Ho sakta
            # hai portal session hi mar gaya ho - safe probe se confirm
            # karo (network hiccup pe alive True hi rakhte hain).
            any_ok = any(
                r.get("success") for r in (tt_result, nt_result, at_result)
            )
            portal_alive = True
            if not any_ok:
                try:
                    probe = scraper.session.get(
                        scraper.auth_url + "frmMyTimeTable.aspx",
                        timeout=12,
                        allow_redirects=True,
                    )
                    if "login" in probe.url.lower():
                        portal_alive = False
                except Exception:
                    pass
            request.session["portal_alive"] = portal_alive
            # (cookies=cookies: store me last-known-good hamesha rahe)
            update_keepalive_state(base_url, uid, cookies=cookies, alive=portal_alive)

            # Cookie jar refresh jo bhi portal ne rotate ki ho
            # (session mar chuka ho to login-page cookies WAPAS save
            # mat karo - purani session cookies hi kaafi hain)
            fresh = requests.utils.dict_from_cookiejar(scraper.session.cookies)
            if fresh and portal_alive:
                state["cookies"] = fresh
                request.session["scraper_state"] = state
                update_keepalive_state(base_url, uid, cookies=fresh)

            request.session["last_data_sync"] = now
            request.session.modified = True
            did_sync = True
            print(
                f"[Sync] realtime rescrape ok - tt={tt_result.get('success')} "
                f"notices={nt_result.get('success')} att={at_result.get('success')} "
                f"alive={portal_alive}"
            )
        except Exception as exc:
            # Sync fail = old data hi dikhao, kabhi session clobber mat karo
            print(f"[Sync] realtime rescrape failed (old data kept): {exc}")

    # ⭐ alive flag: keepalive-thread ki latest state sabse taza hoti
    # hai (usi ping se detect hua ho to); fallback session ki value.
    alive = bool(request.session.get("portal_alive", True))
    if base_url and uid:
        ka_entry = get_keepalive_state(base_url, uid)
        if ka_entry and "alive" in ka_entry:
            alive = bool(ka_entry.get("alive", True))

    records = request.session.get("attendance_data") or []
    total_attended = sum(int(r.get("attended", 0)) for r in records)
    total_held = sum(int(r.get("total", 0)) for r in records)
    # ⭐ v5.6: live payload me bhi avg = active (delivered>0) courses ka
    _pcts = [
        float(r.get("percentage", 0) or 0)
        for r in records
        if int(r.get("total", 0) or 0) > 0
    ]
    avg_percentage = round(sum(_pcts) / len(_pcts), 1) if _pcts else 0
    _portal_overall = request.session.get("attendance_overall")
    if isinstance(_portal_overall, (int, float)) and 0 <= float(_portal_overall) <= 100:
        global_percentage = round(float(_portal_overall), 1)
    else:
        global_percentage = avg_percentage

    return JsonResponse({
        "ok": True,
        "synced": did_sync,
        "ts": now,
        "alive": alive,
        "sig": build_dashboard_sig(request),
        "attendance": {
            "global": global_percentage,
            "attended": total_attended,
            "held": total_held,
            "records": [
                {
                    "attended": r.get("attended", 0),
                    "total": r.get("total", 0),
                    "percentage": r.get("percentage", 0),
                    "miss": r.get("miss", 0),
                    "need": r.get("need", 0),
                }
                for r in records
            ],
        },
    })


def demo_view(request):
    request.session["student_uid"] = "DEMO"
    request.session["attendance_data"] = [
        {
            "code": "DEMO-101",
            "title": "Demo Course",
            "attended": 8,
            "total": 10,
            "percentage": 80.0,
            "miss": 2,
            "need": 0,
        }
    ]
    request.session["available_sessions"] = [
        {"id": "1040_2025_ODD_LKO", "name": "Semester 1", "selected": False},
        {"id": "1041_2025_EVEN_LKO", "name": "Semester 2", "selected": True},
    ]
    request.session["active_session"] = "1041_2025_EVEN_LKO"
    request.session["exam_results"] = [
        {"semester": "Semester 1", "sgpa": "7.04", "cgpa": "7.04"},
        {"semester": "Semester 2", "sgpa": "6.92", "cgpa": "7.04"},
    ]
    request.session["student_cgpa"] = "7.04"
    request.session["subject_grades"] = []
    request.session["marks_data"] = []
    request.session["total_credits"] = 0
    request.session["timetable_data"] = get_mock_timetable()
    request.session["announcements"] = [
        {
            "title": "Mid Semester Examination schedule has been released for all UG programs.",
            "date": "12 Jul 2026",
            "department": "University",
        },
        {
            "title": "Fee payment window for the upcoming session is now open on the student portal.",
            "date": "08 Jul 2026",
            "department": "University",
        },
    ]
    request.session["course_plan"] = {
        "found": True,
        "success": True,
        "source_url": "",
        "page_title": "My Courses",
        "courses": [
            {
                "code": "DEMO-101",
                "title": "Demo Course",
                "meta": ["Theory", "4 Credits"],
                "header": [],
                "plan": [
                    {
                        "heading": "Lecture Plan",
                        "rows": [
                            ["Unit", "Lectures", "Topics"],
                            ["I", "Lec 1-6", "Introduction and fundamentals"],
                            ["II", "Lec 7-14", "Core concepts with examples"],
                        ],
                    }
                ],
                "plan_url": "",
            },
            {
                "code": "DEMO-102",
                "title": "Demo Elective",
                "meta": ["Practical", "2 Credits"],
                "header": [],
                "plan": [],
                "plan_url": "",
            },
        ],
        "extras": [],
    }
    request.session["hostel_details"] = {
        "found": True,
        "source_url": "",
        "page_title": "Student Hostel Detail",
        "kv": [
            {"label": "Hostel Name", "value": "NC-Block (Demo)"},
            {"label": "Room No", "value": "B-214"},
            {"label": "Bed No", "value": "2"},
            {"label": "Allotment Date", "value": "21 Jul 2026"},
            {"label": "Warden", "value": "Mr. Demo Warden"},
            {"label": "Mess", "value": "Veg"},
        ],
        "sections": [
            {
                "heading": "Hostel Fee Detail",
                "rows": [
                    ["Particular", "Amount"],
                    ["Hostel Fee", "96,000"],
                    ["Mess Charges", "36,000"],
                ],
            }
        ],
    }
    request.session["fee_summary"] = {
        "total": "172000", "paid": "86000", "due": "86000",
    }
    request.session["fee_records"] = [
        {
            "semester": "Semester 1",
            "title": "Academic Fee - Odd Semester",
            "amount": "86,000",
            "paid": "86,000",
            "due": "0",
            "date": "12 Aug 2025",
            "status": "PAID",
            "receipt": "RCPT-1041",
        },
        {
            "semester": "Semester 2",
            "title": "Academic Fee - Even Semester",
            "amount": "86,000",
            "paid": "0",
            "due": "86,000",
            "date": "Due by 10 Jan 2026",
            "status": "DUE",
            "receipt": "",
        },
    ]
    return redirect("scraper_app:dashboard")


# ⭐ v5.7 ID CARD: user apna college ID PROFILE tab se upload karke dekhta
# hai. Client-side canvas se compress (<=1280px, JPEG 0.85) karke base64
# JSON aata hai -> Django DB session me store (relogin/restart ke baad bhi
# rahega). Serve dedicated endpoint se (HTML me heavy base64 embed NAHI,
# page halka rehta hai). ?v= ts cache-bust karta hai har change pe.
ID_CARD_MAX_BYTES = 4 * 1024 * 1024        # decoded image cap


def _id_card_json(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return {}


def id_card_upload_view(request):
    """POST {image: "data:image/...;base64,..."} -> session me save."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)
    uid = request.session.get("student_uid")
    if not uid:
        return JsonResponse({"ok": False, "error": "login required"}, status=401)
    data_url = str(_id_card_json(request).get("image") or "")
    if not data_url.startswith("data:image/") or "," not in data_url:
        return JsonResponse({"ok": False, "error": "sirf image file chalegi"})
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except Exception:
        return JsonResponse({"ok": False, "error": "image data corrupt hai"})
    if not raw:
        return JsonResponse({"ok": False, "error": "empty image"})
    if len(raw) > ID_CARD_MAX_BYTES:
        return JsonResponse({"ok": False, "error": "image bahut badi hai"})
    if raw.startswith(b"\xff\xd8\xff"):
        ctype = "image/jpeg"
    elif raw.startswith(b"\x89PNG"):
        ctype = "image/png"
    elif raw.startswith(b"RIFF"):
        ctype = "image/webp"
    else:
        return JsonResponse({"ok": False, "error": "jpeg/png/webp image hi bhejo"})
    request.session["id_card"] = base64.b64encode(raw).decode("ascii")
    request.session["id_card_type"] = ctype
    request.session["id_card_v"] = int(time.time())
    request.session.modified = True
    print(f"[IDCard] upload ok: uid={uid} size={len(raw)//1024}KB type={ctype}")
    return JsonResponse({"ok": True, "v": request.session["id_card_v"]})


def id_card_image_view(request):
    """Session-stored ID card serve karo (img src isko hit karta hai)."""
    b64 = request.session.get("id_card")
    if not b64:
        return HttpResponse(status=404)
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return HttpResponse(status=404)
    resp = HttpResponse(
        raw,
        content_type=request.session.get("id_card_type") or "image/jpeg",
    )
    resp["Cache-Control"] = "private, max-age=60"
    return resp


def id_card_remove_view(request):
    """POST -> ID card hata do."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)
    for key in ("id_card", "id_card_type", "id_card_v"):
        request.session.pop(key, None)
    request.session.modified = True
    print("[IDCard] removed")
    return JsonResponse({"ok": True})


def get_mock_timetable():
    return {"MON": {"day": "MON", "full_day": "MONDAY", "slots": []}}


def get_mock_marks(records):
    return [
        {
            "code": record.get("code", ""),
            "title": record.get("title", ""),
            "marks": [],
        }
        for record in records
    ]
