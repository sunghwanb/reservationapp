import json
import os
from flask import Flask, render_template, request, redirect, url_for, make_response
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote
from flask import send_file
from openpyxl import Workbook
from io import BytesIO

RESERVATION_FILE = "reservations.json"
ADMIN_PASSWORD = "admin1234"

app = Flask(__name__)

reservations = {}
manual_open_slots = set()
manual_close_slots = set()

KST = timezone(timedelta(hours=9))

start_time = datetime.strptime("09:00", "%H:%M").replace(tzinfo=KST)
end_time = datetime.strptime("20:00", "%H:%M").replace(tzinfo=KST)
time_slot_minutes = 5
MAX_PEOPLE_PER_SLOT = 2


def load_reservations():
    global reservations, manual_open_slots, manual_close_slots
    if os.path.exists(RESERVATION_FILE):
        with open(RESERVATION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            reservations = data.get("reservations", {})
            manual_open_slots = set(data.get("manual_open_slots", []))
            manual_close_slots = set(data.get("manual_close_slots", []))


load_reservations()


def save_reservations():
    with open(RESERVATION_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "reservations": reservations,
                "manual_open_slots": list(manual_open_slots),
                "manual_close_slots": list(manual_close_slots)
            },
            f,
            ensure_ascii=False,
            indent=2)


def generate_time_slots():
    now = datetime.now(KST).replace(second=0, microsecond=0)
    slots = []

    today = now.date()
    current = datetime.combine(today, start_time.timetz()).replace(tzinfo=KST)
    end = datetime.combine(today, end_time.timetz()).replace(tzinfo=KST)

    while current < end:
        time_str = current.strftime("%H:%M")
        people = reservations.get(time_str, [])

        auto_open = now <= current <= now + timedelta(hours=1)
        is_open = (auto_open or time_str
                   in manual_open_slots) and time_str not in manual_close_slots
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
    error_message = None  # 💡 요거 무조건 맨 앞에 선언!
    if request.method == "POST" and not user_reserved:
        name = request.form["name"]
        time = request.form["time"]

        if time not in reservations:
            reservations[time] = []

        if len(reservations[time]) < MAX_PEOPLE_PER_SLOT:
            reservations[time].append(name)
            save_reservations()

            resp = make_response(redirect(url_for("index")))
            resp.set_cookie("has_reserved", "true", max_age=60 * 60 * 24 * 7)
            resp.set_cookie("reserved_name",
                            quote(name),
                            max_age=60 * 60 * 24 * 7,
                            samesite="Lax")
            resp.set_cookie("reserved_time",
                            time,
                            max_age=60 * 60 * 24 * 7,
                            samesite="Lax")
            return resp
        else:
            # 실패 시 에러 메시지를 포함해 다시 렌더링
            error_message = "❌ 해당 시간은 인원이 가득 찼습니다.다른 시간대를 선택해주세요."
    time_slots = generate_time_slots()
    return render_template("index.html",
                           time_slots=time_slots,
                           reservations=reservations,
                           user_reserved=user_reserved,
                           name=name,
                           time=time,
                           max_people=MAX_PEOPLE_PER_SLOT,
                           error_message=error_message)


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
                save_reservations()
                message = f"✅ {slot_time} 수동 오픈 완료"
            elif action == "manual_close":
                if slot_time in manual_open_slots:
                    manual_open_slots.remove(slot_time)
                    save_reservations()
                    message = f"🔒 {slot_time} 수동 오픈 해제 완료"
                else:
                    message = f"❌ {slot_time} 는 수동 오픈 상태가 아님"
            elif action == "manual_force_close":
                manual_close_slots.add(slot_time)
                save_reservations()
                message = f"⛔ {slot_time} 자동 오픈 시간 강제 닫기 완료"
            elif action == "cancel_manual_close":
                if slot_time in manual_close_slots:
                    manual_close_slots.remove(slot_time)
                    save_reservations()
                    message = f"✅ {slot_time} 강제 닫힘 취소 완료"
                else:
                    message = f"❌ {slot_time} 는 강제 닫힘 상태가 아님"
            elif action == "delete":
                time = request.form["time"]
                name = request.form["name"]
                if time in reservations and name in reservations[time]:
                    reservations[time].remove(name)
                    save_reservations()
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
                reservations = {}
                manual_open_slots.clear()
                manual_close_slots.clear()
                save_reservations()
                message = "✅ 전체 예약 초기화 완료됨"

    time_slots = generate_time_slots()
    return render_template("admin.html",
                           time_slots=time_slots,
                           reservations=filtered_reservations,
                           max_people=MAX_PEOPLE_PER_SLOT,
                           admin_logged_in=admin_logged_in,
                           message=message,
                           manual_open_slots=manual_open_slots,
                           manual_close_slots=manual_close_slots)


@app.route("/dashboard")
def dashboard():
    now = datetime.now(KST)  # KST 시간대 적용
    time_slots = generate_time_slots()

    # 문자열 시간 -> datetime 객체로 변환 (오늘 날짜 + 시간)
    for slot in time_slots:
        # 시간 문자열 파싱
        time_parts = slot['time'].split(':')
        # 완전한 datetime 객체로 변환 (오늘 날짜 기준)
        slot['time_obj'] = datetime.now(KST).replace(
            hour=int(time_parts[0]), 
            minute=int(time_parts[1]), 
            second=0, 
            microsecond=0
        )

    return render_template(
        "dashboard.html",
        time_slots=time_slots,
        reservations=reservations,
        max_people=MAX_PEOPLE_PER_SLOT,
        now=now
    )

@app.route('/download_excel')
def download_excel():
    wb = Workbook()
    ws = wb.active
    ws.title = "예약 현황"

    # 헤더
    ws.append(["시간", "예약 인원", "상태", "예약자 명단"])

    time_slots = generate_time_slots()  # ✅ 여기에 추가!
    for slot in time_slots:
        time = slot["time"]
        reserved = slot["reserved_count"]
        status = "마감" if reserved >= MAX_PEOPLE_PER_SLOT else "가능"
        names = ", ".join(reservations.get(
            time, [])) if reservations.get(time) else "-"
        ws.append([time, f"{reserved} / {MAX_PEOPLE_PER_SLOT}", status, names])

    # 파일로 내보내기
    output = BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name="체험부스 예약현황.xlsx",
        mimetype=
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=3000)
