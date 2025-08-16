import os
import eventlet
eventlet.monkey_patch()
from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    redirect,
    url_for,
    session,
)
from supabase import create_client, Client
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from functools import wraps
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from flask_socketio import SocketIO, join_room, leave_room, emit
import tempfile
import json
import bcrypt
import httpx 
import logging
import random

load_dotenv()

#! --- Basic app setup ---
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(24)


_httpx_client = httpx.Client(http2=False, timeout=20.0)

#! Create supabase client (service role key for server actions)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

#! SocketIO with eventlet for low-memory async operations
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    logger=False,
    engineio_logger=False,
    max_http_buffer_size=100_000,
)

limiter = Limiter(
    key_func=lambda: session.get("user_id", get_remote_address()),
    app=app,
    default_limits=["100 per minute"],
)

#! Avoid filling up my terminal
logging.getLogger("engineio").setLevel(logging.WARNING)
logging.getLogger("socketio").setLevel(logging.WARNING)



@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

#! --- Helpers & decorators ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated_function


def university_verified_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        account_type = session.get("account_type")
        is_verified = session.get("is_verified")
        if account_type != "university" or is_verified is not True:
            return redirect(request.referrer or "/")
        return f(*args, **kwargs)

    return decorated_function


def university_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))

        account_type = session.get("account_type")
        if account_type != "university":
            return redirect(request.referrer or url_for("home_page"))

        return f(*args, **kwargs)

    return decorated_function


def highschool_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))

        account_type = session.get("account_type")
        if account_type != "student":
            return redirect(url_for("mentor_home_page"))

        return f(*args, **kwargs)

    return decorated_function


def quiz_not_taken_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user_id = session.get("user_id")
        account_type = session.get("account_type")
        if not user_id or account_type != "student":
            return redirect(url_for("login"))
        result = (
            supabase.table("quiz_results")
            .select("id")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if result.data:
            return redirect(url_for("home_page"))
        return f(*args, **kwargs)

    return decorated_function

#! Convert time due to supabase
def parse_iso_to_utc(dt_str: str) -> datetime:
    if not dt_str:
        return None
    try:
        if dt_str.endswith("Z"):
            dt_str = dt_str.replace("Z", "+00:00")
        return datetime.fromisoformat(dt_str).astimezone(timezone.utc)
    except Exception:
        return datetime.strptime(dt_str.split(".")[0], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        )


# !---------- Socket.IO handlers ----------
@socketio.on("connect")
def handle_connect():
    #** get user id from secure flask session (best) or from query string fallback
    user_id = session.get("user_id") or request.args.get("user_id")
    if not user_id:
        # **reject silently: no user id present
        return
    room = str(user_id)
    join_room(room)
    emit("connected", {"ok": True, "user_id": user_id})


@socketio.on("disconnect")
def handle_disconnect():
    user_id = session.get("user_id") or request.args.get("user_id")
    if user_id:
        leave_room(str(user_id))


@socketio.on("send_message")
def handle_send_message(payload):
    """
    Client calls: socket.emit('send_message', { receiver_id, message }, ack_cb)
    Insert or update a single message row to avoid duplicates within a 4-second window.
    """
    try:
        sender_id = session.get("user_id")
        if not sender_id:
            return emit("error", {"error": "Not authenticated"})

        receiver_id = payload.get("receiver_id")
        message_text = (payload.get("message") or "").strip()
        if not receiver_id or not message_text:
            return emit("error", {"error": "Missing fields"})

        #! Authorization guard: check mentor-student link depending on account type
        account_type = session.get("account_type")
        if account_type == "student":
            check = (
                supabase.table("mentor_student_links")
                .select("approved")
                .eq("student_id", sender_id)
                .eq("mentor_id", receiver_id)
                .limit(1)
                .execute()
            )
            if not check.data or not check.data[0].get("approved", False):
                return emit("error", {"error": "Not authorized to send to this mentor."})
        elif account_type == "university":
            check = (
                supabase.table("mentor_student_links")
                .select("approved")
                .eq("mentor_id", sender_id)
                .eq("student_id", receiver_id)
                .limit(1)
                .execute()
            )
            if not check.data or not check.data[0].get("approved", False):
                return emit("error", {"error": "Not authorized to send to this student."})

        # Dedupe / coalesce logic:
        # *If there's a most recent message from sender->receiver within the last 4s:
        #   * if its text == new text -> skip insert, return that existing row
        #   * else -> update that row with new text and updated_at (so only one DB row remains per 4s window)
        four_seconds_ago = datetime.now(timezone.utc) - timedelta(seconds=4)

        recent = (
            supabase.table("messages")
            .select("id, created_at, message, sender_id, receiver_id")
            .eq("sender_id", sender_id)
            .eq("receiver_id", receiver_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )

        if hasattr(recent, "data") and recent.data:
            last = recent.data[0]
            last_time = parse_iso_to_utc(last.get("created_at"))
            if last_time and last_time >= four_seconds_ago:
                #! within 4 seconds
                if (last.get("message") or "").strip() == message_text:
                    # !identical message within window -> return existing row (no DB write)
                    emit("send_ack", {"ok": True, "row": last})
                    # !emit to rooms to ensure both sender & receiver see it
                    socketio.emit("new_message", last, room=str(receiver_id))
                    socketio.emit("new_message", last, room=str(sender_id))
                    return
                else:
                    # !update last message to newest text (coalesce)
                    upd = (
                        supabase.table("messages")
                        .update({"message": message_text, "created_at": datetime.now(timezone.utc).isoformat()})
                        .eq("id", last["id"])
                        .execute()
                    )
                    if isinstance(upd, dict) and upd.get("error"):
                        return emit("error", {"error": upd["error"]["message"]})
                    updated_row = upd.data[0] if hasattr(upd, "data") and upd.data else None
                    socketio.emit("new_message", updated_row, room=str(receiver_id))
                    socketio.emit("new_message", updated_row, room=str(sender_id))
                    return emit("send_ack", {"ok": True, "row": updated_row})

        # !Otherwise: insert a fresh message row
        insert_data = {
            "sender_id": sender_id,
            "receiver_id": receiver_id,
            "message": message_text,
        }
        r = supabase.table("messages").insert(insert_data).execute()
        if isinstance(r, dict) and r.get("error"):
            return emit("error", {"error": r["error"]["message"]})

        row = r.data[0] if hasattr(r, "data") and r.data else None
        if not row:
            return emit("error", {"error": "Insert failed"})

        socketio.emit("new_message", row, room=str(receiver_id))
        socketio.emit("new_message", row, room=str(sender_id))
        emit("send_ack", {"ok": True, "row": row})

    except Exception as e:
        emit("error", {"error": str(e)})


@socketio.on("typing")
def handle_typing(payload):
    try:
        from_id = session.get("user_id")
        to_id = payload.get("to_id")
        is_typing = bool(payload.get("is_typing"))
        if not from_id or not to_id:
            return
        # !keep a small typing status table update
        q = (
            supabase.table("typing_status")
            .select("id")
            .eq("from_id", from_id)
            .eq("to_id", to_id)
            .limit(1)
            .execute()
        )

        if hasattr(q, "data") and q.data:
            supabase.table("typing_status").update(
                {"is_typing": is_typing, "updated_at": "now()"}
            ).eq("from_id", from_id).eq("to_id", to_id).execute()
        else:
            supabase.table("typing_status").insert(
                {"from_id": from_id, "to_id": to_id, "is_typing": is_typing}
            ).execute()

        socketio.emit(
            "typing_update",
            {"from_id": from_id, "to_id": to_id, "is_typing": is_typing},
            room=str(to_id),
        )
        emit("typing_ack", {"ok": True})
    except Exception as e:
        emit("error", {"error": str(e)})


@socketio.on("mark_seen")
def handle_mark_seen(payload):
    try:
        user_id = session.get("user_id")
        other_id = payload.get("other_id")
        if not user_id or not other_id:
            return emit("error", {"error": "Missing fields"})

        r = (
            supabase.table("messages")
            .update({"seen": True})
            .eq("receiver_id", user_id)
            .eq("sender_id", other_id)
            .eq("seen", False)
            .execute()
        )

        socketio.emit(
            "messages_seen", {"by": user_id, "other_id": other_id}, room=str(other_id)
        )

        emit(
            "mark_seen_ack",
            {
                "ok": True,
                "updated": len(r.data) if hasattr(r, "data") and r.data else 0,
            },
        )
    except Exception as e:
        emit("error", {"error": str(e)})


#! ---------- HTTP endpoints ----------
@app.route("/")
def main_page():
    return render_template("index.html")


@app.route("/policy")
def policy_page():
    return render_template("policy.html")


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        if not email or not password:
            return render_template(
                "log-in.html", error="Please provide both email and password."
            )

        user_data = supabase.table("users").select("*").eq("email", email).single().execute()
        if not user_data.data:
            return render_template("log-in.html", error="Incorrect Email or Password.")

        user = user_data.data
        if bcrypt.checkpw(password.encode("utf-8"), user["password"].encode("utf-8")):
            session["user_id"] = user["id"]
            session["is_verified"] = user.get("is_verified", False)
            session["account_type"] = user.get("account_type", "").strip().lower()

            if session["account_type"] == "university":
                return redirect("/home") if session["is_verified"] else redirect("/verify")
            elif session["account_type"] == "student":
                return redirect("/questions")
            return redirect("/home")
        return render_template("log-in.html", error="Incorrect Email or Password.")
    return render_template("log-in.html")


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def register():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")
        name = request.form.get("name")
        account_type = (request.form.get("account_type") or "").strip().lower()
        existing_user = supabase.table("users").select("id").eq("email", email).execute()
        if existing_user.data:
            return render_template("register.html", error="This email is already registered.")

        hashed_password = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        is_verified = None if account_type == "student" else False

        supabase.table("users").insert({
            "email": email,
            "password": hashed_password,
            "name": name,
            "account_type": account_type,
            "is_verified": is_verified
        }).execute()

        user_data = supabase.table("users").select("*").eq("email", email).single().execute()
        user = user_data.data
        session["user_id"] = user["id"]
        session["is_verified"] = user.get("is_verified", False)
        session["account_type"] = account_type

        return redirect("/questions") if account_type == "student" else redirect("/verify")
    return render_template("register.html")


@app.route("/verify", methods=["GET", "POST"])
@login_required
@university_required
def verify_page():
    user_id = session.get("user_id")
    user_data = supabase.table("users").select("name").eq("id", user_id).single().execute()
    name = user_data.data["name"] if user_data.data else "User"

    if request.method == "POST":
        file = request.files.get("verification_image")
        if not file:
            return render_template("verify.html", name=name, error="No file uploaded.")

        try:
            #! lazy import PIL/pytesseract to reduce memory at startup
            from PIL import Image
            import pytesseract

            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as temp:
                file.save(temp.name)
                text = pytesseract.image_to_string(Image.open(temp.name))

            if any(keyword.lower() in text.lower() for keyword in ["university", "student", "college"]):
                supabase.table("users").update({"is_verified": True}).eq("id", user_id).execute()
                session["is_verified"] = True
                return redirect("/mentor_home")
            else:
                return render_template("verify.html", name=name, error="Verification failed. Try a clearer image.")
        except Exception as e:
            return render_template("verify.html", name=name, error=f"Error during verification: {str(e)}")

    return render_template("verify.html", name=name)


@app.route("/questions", methods=["GET", "POST"])
@quiz_not_taken_required
@highschool_required
def questions():
    if request.method == "POST":
        user_id = session.get("user_id")
        data = request.json or {}
        result = data.get("result")
        interest_scores = data.get("scores")
        if not result or not isinstance(interest_scores, dict):
            return {"error": "Invalid quiz data"}, 400

        supabase.table("quiz_results").insert({
            "user_id": user_id,
            "main_interest": result,
            "interest_scores": json.dumps(interest_scores),
            "quiz_taken_at": datetime.utcnow().isoformat()
        }).execute()
        return {"message": "Quiz result saved successfully"}, 200
    return render_template("questions.html")


@app.route("/home")
@login_required
@highschool_required
def home_page():
    user_id = session.get("user_id")
    user_data = supabase.table("users").select("name").eq("id", user_id).single().execute()
    name = user_data.data["name"] if user_data.data else "User"
    return render_template("home.html", name=name)


@app.route("/courses")
@login_required
@highschool_required
def course_page():
    return render_template("courses.html")


@app.route("/explore")
@login_required
@highschool_required
def explore_page():
    return render_template("explore.html")


@app.route("/mentors")
@login_required
@highschool_required
def mentors_page():
    return render_template("mentors.html")


@app.route("/messages")
@login_required
@highschool_required
def messages_page():
    return render_template(
        "messages.html",
        supabase_url=SUPABASE_URL,
        supabase_anon_key=SUPABASE_ANON_KEY,
        my_user_id=session.get("user_id"),
    )


@app.route("/mentor_messages")
@login_required
@university_verified_required
def mentor_messages_page():
    return render_template(
        "mentor_messages.html",
        supabase_url=SUPABASE_URL,
        supabase_anon_key=SUPABASE_ANON_KEY,
        my_user_id=session.get("user_id"),
    )


@app.route("/mentor_home")
@university_verified_required
@login_required
def mentor_home_page():
    user_id = session.get("user_id")
    user_data = supabase.table("users").select("name").eq("id", user_id).single().execute()
    name = user_data.data["name"] if user_data.data else "User"
    return render_template("mentor_home.html", name=name)


@app.route("/mentees")
@university_verified_required
@login_required
def mentees_page():
    return render_template("mentees.html")


@app.route("/mentor_analytics")
@university_verified_required
@login_required
def mentor_analytics_page():
    return render_template("mentor_analytics.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

#! API's starting from here

@app.route("/api/my_mentor")
@login_required
@highschool_required
def api_my_mentor():
    student_id = session.get("user_id")
    
    # !Get all mentor links for the student
    res = (
        supabase.table("mentor_student_links")
        .select("mentor_id, approved")
        .eq("student_id", student_id)
        .execute()
    )

    if not res.data or len(res.data) == 0:
        return jsonify({"data": []}), 200

    mentors_data = []

    for rec in res.data:
        mentor_id = rec.get("mentor_id")
        approved = rec.get("approved", False)

        if not mentor_id:
            continue

        mentor_res = (
            supabase.table("users")
            .select("id, name, email, account_type")
            .eq("id", mentor_id)
            .single()
            .execute()
        )

        if mentor_res.data:
            mentors_data.append({
                "mentor": mentor_res.data,
                "approved": approved
            })

    return jsonify({"data": mentors_data}), 200

@app.route("/api/my_requests")
@login_required
@university_verified_required
def api_my_requests():
    mentor_id = session.get("user_id")
    res = (supabase.table("mentor_student_links")
           .select("student_id,approved,created_at")
           .eq("mentor_id", mentor_id)
           .order("created_at", desc=False)
           .execute())
    if isinstance(res, dict) and res.get("error"):
        return jsonify({"error": res["error"]["message"]}), 500

    student_ids = [r["student_id"] for r in res.data] if res.data else []
    if not student_ids:
        return jsonify({"data": []})

    students = supabase.table("users").select("id,name,email,account_type").in_("id", student_ids).execute()
    if isinstance(students, dict) and students.get("error"):
        return jsonify({"error": students["error"]["message"]}), 500

    by_id = {s["id"]: s for s in (students.data or [])}
    out = []
    for rec in res.data:
        sid = rec["student_id"]
        out.append({"student": by_id.get(sid), "approved": rec.get("approved", False), "created_at": rec.get("created_at")})

    return jsonify({"data": out})


@app.route("/api/approve_student", methods=["POST"])
@login_required
@university_verified_required
def api_approve_student():
    payload = request.get_json() or {}
    student_id = payload.get("student_id")
    mentor_id = session.get("user_id")

    if not student_id:
        return jsonify({"error": "Missing student_id"}), 400

    r = supabase.table("mentor_student_links").update({"approved": True}).eq("mentor_id", mentor_id).eq("student_id", student_id).execute()
    if isinstance(r, dict) and r.get("error"):
        return jsonify({"error": r["error"]["message"]}), 500
    return jsonify({"ok": True})


@app.route("/api/messages/<other_id>")
@login_required
def api_messages_with(other_id):
    try:
        user_id = session.get("user_id")
        account_type = session.get("account_type")

        # Authorization:
        if account_type == "student":
            check = supabase.table("mentor_student_links").select("id,approved").eq("student_id", user_id).eq("mentor_id", other_id).limit(1).execute()
            if not check.data or not check.data[0].get("approved", False):
                return jsonify({"error": "Not authorized to view messages with this user."}), 403
        elif account_type == "university":
            check = supabase.table("mentor_student_links").select("id,approved").eq("mentor_id", user_id).eq("student_id", other_id).limit(1).execute()
            if not check.data or not check.data[0].get("approved", False):
                return jsonify({"error": "Not authorized to view messages with this user."}), 403

        expr = f"or(and(sender_id.eq.{user_id},receiver_id.eq.{other_id}),and(sender_id.eq.{other_id},receiver_id.eq.{user_id}))"
        msgs = supabase.table("messages").select("*").or_(expr).order("created_at", desc=False).execute()
        if isinstance(msgs, dict) and msgs.get("error"):
            return jsonify({"error": msgs["error"]["message"]}), 500

        raw_data = msgs.data if hasattr(msgs, "data") else []
        seen = set()
        unique_msgs = []
        for m in raw_data:
            #? dedupe using created_at (string), sender, receiver, message
            key = (m.get("created_at"), m.get("sender_id"), m.get("receiver_id"), m.get("message"))
            if key not in seen:
                seen.add(key)
                unique_msgs.append(m)

        return jsonify({"data": unique_msgs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/messages/send", methods=["POST"])
@login_required
def api_send_message():
    """
    HTTP endpoint variant for inserting messages (keeps parity with socket handler).
    Uses same de-duplication/coalescing logic to avoid duplicate rows.
    """
    try:
        payload = request.get_json() or {}
        sender_id = session.get("user_id")
        receiver_id = payload.get("receiver_id")
        message = (payload.get("message") or "").strip()

        if not receiver_id or not message:
            return jsonify({"error": "Missing receiver_id or message"}), 400

        account_type = session.get("account_type")
        if account_type == "student":
            check = supabase.table("mentor_student_links").select("approved").eq("student_id", sender_id).eq("mentor_id", receiver_id).limit(1).execute()
            if not check.data or not check.data[0].get("approved", False):
                return jsonify({"error": "Not authorized to send to this mentor."}), 403
        elif account_type == "university":
            check = supabase.table("mentor_student_links").select("approved").eq("mentor_id", sender_id).eq("student_id", receiver_id).limit(1).execute()
            if not check.data or not check.data[0].get("approved", False):
                return jsonify({"error": "Not authorized to send to this student."}), 403

        #! duplicate/coalesce behavior
        four_seconds_ago = datetime.now(timezone.utc) - timedelta(seconds=4)
        recent = supabase.table("messages").select("id,created_at,message").eq("sender_id", sender_id).eq("receiver_id", receiver_id).order("created_at", desc=True).limit(1).execute()

        if hasattr(recent, "data") and recent.data:
            last = recent.data[0]
            last_time = parse_iso_to_utc(last.get("created_at"))
            if last_time and last_time >= four_seconds_ago:
                # !identical message -> skip
                if (last.get("message") or "").strip() == message:
                    return jsonify({"ok": True, "row": last})
                #! else update the last row's message (coalesce)
                upd = supabase.table("messages").update({"message": message, "created_at": datetime.now(timezone.utc).isoformat()}).eq("id", last["id"]).execute()
                if isinstance(upd, dict) and upd.get("error"):
                    return jsonify({"error": upd["error"]["message"]}), 500
                updated_row = upd.data[0] if hasattr(upd, "data") and upd.data else None
                socketio.emit("new_message", updated_row, room=str(receiver_id))
                socketio.emit("new_message", updated_row, room=str(sender_id))
                return jsonify({"ok": True, "row": updated_row})

        #! otherwise insert new
        insert_data = {"sender_id": sender_id, "receiver_id": receiver_id, "message": message}
        r = supabase.table("messages").insert(insert_data).execute()
        if isinstance(r, dict) and r.get("error"):
            return jsonify({"error": r["error"]["message"]}), 500

        row = r.data[0] if hasattr(r, "data") and r.data else None
        socketio.emit("new_message", row, room=str(receiver_id))
        socketio.emit("new_message", row, room=str(sender_id))
        return jsonify({"ok": True, "row": row})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/typing", methods=["POST"])
@login_required
def api_typing():
    try:
        payload = request.get_json() or {}
        to_id = payload.get("to_id")
        is_typing = bool(payload.get("is_typing"))
        user_id = session.get("user_id")
        if not to_id:
            return jsonify({"error": "Missing to_id"}), 400

        q = supabase.table("typing_status").select("id").eq("from_id", user_id).eq("to_id", to_id).limit(1).execute()
        if hasattr(q, "data") and q.data:
            supabase.table("typing_status").update({"is_typing": is_typing, "updated_at": "now()"}).eq("from_id", user_id).eq("to_id", to_id).execute()
        else:
            supabase.table("typing_status").insert({"from_id": user_id, "to_id": to_id, "is_typing": is_typing}).execute()

        socketio.emit("typing_update", {"from_id": user_id, "to_id": to_id, "is_typing": is_typing}, room=str(to_id))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/messages/mark_seen/<other_id>", methods=["POST"])
@login_required
def api_mark_seen(other_id):
    try:
        user_id = session.get("user_id")
        r = supabase.table("messages").update({"seen": True}).eq("receiver_id", user_id).eq("sender_id", other_id).eq("seen", False).execute()
        socketio.emit("messages_seen", {"by": user_id, "other_id": other_id}, room=str(other_id))
        updated_count = len(r.data) if hasattr(r, "data") and r.data else 0
        return jsonify({"ok": True, "updated": updated_count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/me")
@login_required
def api_me():
    try:
        user_id = session.get("user_id")
        r = supabase.table("users").select("id,name,email,account_type").eq("id", user_id).single().execute()
        if isinstance(r, dict) and r.get("error"):
            return jsonify({"error": r["error"]["message"]}), 500
        return jsonify({"data": r.data if hasattr(r, "data") else None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
@app.route("/api/university_verified_users")
@login_required
@highschool_required
def api_university_verified_users():
    try:
        #! Get the current logged-in student ID
        student_id = session.get("user_id")
        if not student_id:
            return jsonify({"error": "No student ID found in session"}), 400

        # !Step 1: Get mentor IDs linked to this student (approved only)
        links_res = (
            supabase.table("mentor_student_links")
            .select("mentor_id")
            .eq("student_id", student_id)
            .eq("approved", True)
            .execute()
        )
        mentor_ids = [link["mentor_id"] for link in links_res.data] if links_res.data else []

        if not mentor_ids:
            return jsonify({"data": []})  #! No linked mentors

        #! Step 2: Get university mentors info from users table
        mentors_res = (
            supabase.table("users")
            .select("id", "name", "account_type", "is_verified", "created_at")
            .in_("id", mentor_ids)
            .eq("account_type", "university")
            .eq("is_verified", True)
            .execute()
        )
        mentors = mentors_res.data if hasattr(mentors_res, "data") else []

        # !Step 3: Get courses for those mentors from mentor_courses table
        courses_res = (
            supabase.table("mentor_courses")
            .select("mentor_id", "course_name")
            .in_("mentor_id", mentor_ids)
            .execute()
        )
        courses = courses_res.data if hasattr(courses_res, "data") else []

        #! Step 4: Map mentor_id -> list of courses
        mentor_courses_map = {}
        for c in courses:
            mentor_courses_map.setdefault(c["mentor_id"], []).append(c["course_name"])

        # !Step 5: Add courses list to each mentor's data
        for mentor in mentors:
            mentor["courses"] = mentor_courses_map.get(mentor["id"], [])

        return jsonify({"data": mentors})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/request_mentor", methods=["POST"])
@login_required
@highschool_required
def api_request_mentor():
    try:
        payload = request.get_json() or {}
        mentor_id = payload.get("mentor_id")
        student_id = session.get("user_id")

        if not mentor_id:
            return jsonify({"error": "Missing mentor_id"}), 400

        #! Check if link already exists
        existing_link = supabase.table("mentor_student_links") \
            .select("*") \
            .eq("mentor_id", mentor_id) \
            .eq("student_id", student_id) \
            .execute()

        if existing_link.data:
            #! Update existing record
            supabase.table("mentor_student_links") \
                .update({"approved": True}) \
                .eq("mentor_id", mentor_id) \
                .eq("student_id", student_id) \
                .execute()
        else:
            # !Insert new record
            supabase.table("mentor_student_links").insert({
                "mentor_id": mentor_id,
                "student_id": student_id,
                "approved": True
            }).execute()

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/courses")
@login_required
@highschool_required
def api_courses():
    try:
        #! Get all courses and their mentors
        res = (
            supabase.table("mentor_courses")
            .select("id, course_name, description, mentor_id, users(name)")
            .execute()
        )
        return jsonify({"data": res.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
@app.route("/api/assign_mentor", methods=["POST"])
@login_required
@highschool_required
def assign_mentor():
    try:
        student_id = session.get("user_id")
        if not student_id:
            return jsonify({"error": "User not logged in properly"}), 401

        data = request.json or {}
        course_name = data.get("course_name")
        if not course_name:
            return jsonify({"error": "Course name is required"}), 400

        #! Get all mentors teaching this course
        mentors_res = supabase.table("mentor_courses") \
            .select("mentor_id") \
            .eq("course_name", course_name) \
            .execute()

        if not mentors_res.data:
            return jsonify({"error": "No mentors found for this course"}), 404

        mentor_ids = [m["mentor_id"] for m in mentors_res.data]
        chosen_mentor = random.choice(mentor_ids)

        #! Insert link in mentor_student_links, approve immediately
        supabase.table("mentor_student_links").insert({
            "student_id": student_id,
            "mentor_id": chosen_mentor,
            "approved": True
        }).execute()

        #! Return mentor_id so JS redirects
        return jsonify({"mentor_id": chosen_mentor})

    except Exception as e:
        import traceback
        print("Error in /api/assign_mentor:", e)
        traceback.print_exc()
        return jsonify({"error": f"Internal error: {str(e)}"}), 500

if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() in ("1", "true")
    socketio.run(app, debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
