import json
import os
from flask import Flask, render_template, request, redirect, url_for, make_response
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote
from flask import send_file
from openpyxl import Workbook
from io import BytesIO
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()  # 로컬 개발 시 .env 파일 불러오기

SUPABASE_URL = os.environ.get("https://lnmhamtoqdmlytivhxfi.supabase.co")
SUPABASE_KEY = os.environ.get("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImxubWhhbXRvcWRtbHl0aXZoeGZpIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDczMTc1OTgsImV4cCI6MjA2Mjg5MzU5OH0.pAnHgJE7J8Z1DopYsV4aWNbkleJzykvMWmg1C_chbRc")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123123!")

app = Flask(__name__)

reservations = {}
manual_open_slots = set()
manual_close_slots = set()

KST = timezone(timedelta(hours=9))

start_time = datetime.strptime("08:50", "%H:%M").replace(tzinfo=KST)
end_time = datetime.strptime("17:00", "%H:%M").replace(tzinfo=KST)
time_slot_minutes = 5
MAX_PEOPLE_PER_SLOT = 2

def load_reservations_from_supabase():
    global reservations, manual_open_slots, manual_close_slots

    reservations.clear()
    res = supabase.table("reservations").select("*").execute()
    for r in res.data:
        time = r["time"]
        name = r["name"]
        reservations.setdefault(time, []).append(name)

    manual_open_slots.clear()
    manual_close_slots.clear()
    settings = supabase.table("manual_settings").select("*").execute()
    for s in settings.data:
        if s.get("manual_open"):
            manual_open_slots.add(s["slot_time"])
        if s.get("manual_close"):
            manual_close_slots.add(s["slot_time"])

def save_reservations_to_supabase():
    supabase.table("reservations").delete().neq("time", "").execute()
    data = [
        {"time": time, "name": name}
        for time, names in reservations.items()
        for name in names
    ]
    if data:
        supabase.table("reservations").insert(data).execute()

    supabase.table("manual_settings").delete().neq("slot_time", "").execute()
    manual_data = []
    for slot in manual_open_slots:
        manual_data.append({"slot_time": slot, "manual_open": True})
    for slot in manual_close_slots:
        existing = next((m for m in manual_data if m["slot_time"] == slot), None)
        if existing:
            existing["manual_close"] = True
        else:
            manual_data.append({"slot_time": slot, "manual_close": True})
    if manual_data:
        supabase.table("manual_settings").insert(manual_data).execute()

def generate_time_slots():
    now = datetime.now(KST).replace(second=0, microsecond=0)
    slots = []

    today = now.date()
    current = datetime.combine(today, start_time.timetz()).replace(tzinfo=KST)
    end = datetime.combine(today, end_time.timetz()).replace(tzinfo=KST)

    while current < end:
        time_str = current.strftime("%H:%M")
        people = reservations.get(time_str, [])

        auto_open = now <= current <= now + timedelta(hours=0.5)
        is_open = (auto_open or time_str in manual_open_slots) and time_str not in manual_close_slots
        is_full = len(people) >= MAX_PEOPLE_PER_SLOT
        is_closed = current < now
        is_force_closed = time_str in manual_close_slots

        slots.append({
            "time": time_str,
            "reserved_count": len(people),
            "available": len(people) < MAX_PEOPLE_PER_SLOT and is_open,
            "is_full": is_full,
            "is_closed": is_closed,
            "is_force_closed": is_force_closed,
        })

        current += timedelta(minutes=time_slot_minutes)

    return slots

@app.route("/", methods=["GET", "POST"])
def index():
    user_reserved = request.cookies.get("has_reserved")
    name = unquote(request.cookies.get("reserved_name", ""))
    time = request.cookies.get("reserved_time", "")
    error_message = None

    if request.method == "POST" and not user_reserved:
        name = request.form["name"]
        time = request.form["time"]

        if time not in reservations:
            reservations[time] = []

        if len(reservations[time]) < MAX_PEOPLE_PER_SLOT:
            reservations[time].append(name)
            save_reservations_to_supabase()

            resp = make_response(redirect(url_for("index")))
            resp.set_cookie("has_reserved", "true", max_age=60 * 60 * 24 * 7)
            resp.set_cookie("reserved_name", quote(name), max_age=60 * 60 * 24 * 7, samesite="Lax")
            resp.set_cookie("reserved_time", time, max_age=60 * 60 * 24 * 7, samesite="Lax")
            return resp
        else:
            error_message = "❌ 해당 시간은 인원이 가득 찼습니다. 다른 시간대를 선택해주세요."

    time_slots = generate_time_slots()
    return render_template("index.html", time_slots=time_slots, reservations=reservations,
                           user_reserved=user_reserved, name=name, time=time,
                           max_people=MAX_PEOPLE_PER_SLOT, error_message=error_message)

@app.route("/cc")
def clear_cookie():
    resp = make_response("✅ 쿠키 삭제 완료. 이제 다시 예약할 수 있습니다.")
    resp.delete_cookie("has_reserved")
    resp.delete_cookie("reserved_name")
    resp.delete_cookie("reserved_time")
    return resp

@app.route("/admin", methods=["GET", "POST"])
def admin():
    global reservations, manual_open_slots, manual_close_slots

    admin_logged_in = request.cookies.get("admin_auth") == "true"
    message = ""
    filtered_reservations = reservations
    action = request.form.get("action") if request.method == "POST" else None

    if request.method == "POST":
        if not admin_logged_in:
            password = request.form.get("password")
            if password == ADMIN_PASSWORD:
                resp = make_response(redirect(url_for("admin")))
                resp.set_cookie("admin_auth", "true", max_age=60 * 60)
                return resp
            else:
                message = "❌ 비밀번호가 틀렸습니다."
        else:
            slot_time = request.form.get("slot_time")
            if action == "manual_open":
                manual_open_slots.add(slot_time)
                save_reservations_to_supabase()
                message = f"✅ {slot_time} 수동 오픈 완료"
            elif action == "manual_close":
                manual_open_slots.discard(slot_time)
                save_reservations_to_supabase()
                message = f"🔒 {slot_time} 수동 오픈 해제 완료"
            elif action == "manual_force_close":
                manual_close_slots.add(slot_time)
                save_reservations_to_supabase()
                message = f"⛔ {slot_time} 자동 오픈 시간 강제 닫기 완료"
            elif action == "cancel_manual_close":
                manual_close_slots.discard(slot_time)
                save_reservations_to_supabase()
                message = f"✅ {slot_time} 강제 닫힘 취소 완료"
            elif action == "delete":
                time = request.form["time"]
                name = request.form["name"]
                if time in reservations and name in reservations[time]:
                    reservations[time].remove(name)
                    save_reservations_to_supabase()
                    message = f"✅ {time} 예약자 {name} 삭제됨"
                else:
                    message = "❌ 해당 예약자 없음"
            elif action == "search":
                keyword = request.form["keyword"]
                filtered_reservations = {
                    time: [n for n in names if keyword in n]
                    for time, names in reservations.items()
                    if any(keyword in n for n in names)
                }
            elif action == "reset":
                reservations.clear()
                manual_open_slots.clear()
                manual_close_slots.clear()
                save_reservations_to_supabase()
                message = "✅ 전체 예약 초기화 완료됨"

    time_slots = generate_time_slots()
    return render_template("admin.html", time_slots=time_slots, reservations=filtered_reservations,
                           max_people=MAX_PEOPLE_PER_SLOT, admin_logged_in=admin_logged_in,
                           message=message, manual_open_slots=manual_open_slots,
                           manual_close_slots=manual_close_slots)

@app.route("/dashboard")
def dashboard():
    now = datetime.now(KST)
    time_slots = generate_time_slots()
    for slot in time_slots:
        time_parts = slot['time'].split(':')
        slot['time_obj'] = now.replace(hour=int(time_parts[0]), minute=int(time_parts[1]), second=0, microsecond=0)

    return render_template("dashboard.html", time_slots=time_slots, reservations=reservations,
                           max_people=MAX_PEOPLE_PER_SLOT, now=now)

@app.route('/download_excel')
def download_excel():
    wb = Workbook()
    ws = wb.active
    ws.title = "예약 현황"

    ws.append(["시간", "예약 인원", "상태", "예약자 명단"])
    time_slots = generate_time_slots()
    for slot in time_slots:
        time = slot["time"]
        reserved = slot["reserved_count"]
        status = "마감" if reserved >= MAX_PEOPLE_PER_SLOT else "가능"
        names = ", ".join(reservations.get(time, [])) or "-"
        ws.append([time, f"{reserved} / {MAX_PEOPLE_PER_SLOT}", status, names])

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(output, as_attachment=True, download_name="체험부스_예약현황.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

if __name__ == "__main__":
    load_reservations_from_supabase()
    app.run(debug=True, host="0.0.0.0", port=3000)
