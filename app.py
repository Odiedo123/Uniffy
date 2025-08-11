from flask import Flask, request, send_from_directory, jsonify, render_template, redirect, url_for, session, Response, make_response, send_file
from supabase import create_client, Client
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from functools import wraps
from datetime import datetime
from dotenv import load_dotenv
from PIL import Image
from flask_socketio import SocketIO, join_room, leave_room, emit
import pytesseract
import tempfile
import os
import json
import bcrypt
import httpx

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

client_options = {
    "http_client": httpx.Client(http2=False)
}

socketio = SocketIO(app, cors_allowed_origins="*")

limiter = Limiter(
    key_func=lambda: session.get("user_id", get_remote_address()),
    app=app,
    default_limits=["100 per minute"]
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")  # Use service role key to enable RLS access with policies
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def university_verified_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        account_type = session.get('account_type')
        is_verified = session.get('is_verified')
        if account_type != 'university' or is_verified is not True:
            return redirect(request.referrer or '/')
        return f(*args, **kwargs)
    return decorated_function

def university_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        account_type = session.get('account_type')
        if account_type != 'university':
            return redirect(request.referrer or url_for('home_page'))

        return f(*args, **kwargs)
    return decorated_function

def highschool_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        account_type = session.get('account_type')
        if account_type != 'student':
            return redirect(url_for('mentor_home_page'))

        return f(*args, **kwargs)
    return decorated_function


def quiz_not_taken_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user_id = session.get('user_id')
        account_type = session.get('account_type')
        if not user_id or account_type != 'student':
            return redirect(url_for('login'))
        result = supabase.table('quiz_results').select('id').eq('user_id', user_id).limit(1).execute()
        if result.data:
            return redirect(url_for('home_page'))
        return f(*args, **kwargs)
    return decorated_function


# ---------- Socket.IO handlers ----------
@socketio.on('connect')
def handle_connect():
    # try to fetch user id from flask session first (secure), fallback to query param
    user_id = session.get('user_id') or request.args.get('user_id')
    if not user_id:
        # no user id — reject connection (optional)
        # You may want to allow anonymous read-only connections; here we just join if present.
        return
    # ensure room is string
    room = str(user_id)
    join_room(room)
    emit('connected', {'ok': True, 'user_id': user_id})
    # optional: you could emit a presence event here


@socketio.on('disconnect')
def handle_disconnect():
    user_id = session.get('user_id') or request.args.get('user_id')
    if user_id:
        leave_room(str(user_id))


@socketio.on('send_message')
def handle_send_message(payload):
    """
    Client calls: socket.emit('send_message', { receiver_id, message }, ack_cb)
    Server inserts message into Supabase, then emits 'new_message' to receiver room and to sender room.
    """
    try:
        sender_id = session.get('user_id')
        if not sender_id:
            return emit('error', {'error': 'Not authenticated'})

        receiver_id = payload.get('receiver_id')
        message_text = (payload.get('message') or '').strip()
        if not receiver_id or not message_text:
            return emit('error', {'error': 'Missing fields'})

        insert_data = {
            "sender_id": sender_id,
            "receiver_id": receiver_id,
            "message": message_text
        }
        r = supabase.table('messages').insert(insert_data).execute()
        if isinstance(r, dict) and r.get("error"):
            return emit('error', {'error': r['error']['message']})

        row = r.data[0] if hasattr(r, "data") and r.data else None
        if not row:
            return emit('error', {'error': 'Insert failed'})

        # broadcast to receiver's room and sender's room
        socketio.emit('new_message', row, room=str(receiver_id))
        socketio.emit('new_message', row, room=str(sender_id))

        # acknowledgement to the emitter (optional)
        emit('send_ack', {'ok': True, 'row': row})
    except Exception as e:
        emit('error', {'error': str(e)})


@socketio.on('typing')
def handle_typing(payload):
    """
    payload: { to_id: 'otherUserId', is_typing: true/false }
    """
    try:
        from_id = session.get('user_id')
        to_id = payload.get('to_id')
        is_typing = bool(payload.get('is_typing'))
        if not from_id or not to_id:
            return
        # update DB as you did in HTTP API (keeps history)
        q = supabase.table('typing_status') \
            .select('id') \
            .eq('from_id', from_id) \
            .eq('to_id', to_id) \
            .limit(1) \
            .execute()

        if hasattr(q, "data") and q.data:
            r = supabase.table('typing_status') \
                .update({
                    'is_typing': is_typing,
                    'updated_at': 'now()'
                }) \
                .eq('from_id', from_id) \
                .eq('to_id', to_id) \
                .execute()
        else:
            r = supabase.table('typing_status') \
                .insert({
                    'from_id': from_id,
                    'to_id': to_id,
                    'is_typing': is_typing
                }) \
                .execute()

        # emit realtime typing update to the recipient's room
        socketio.emit('typing_update', {
            'from_id': from_id,
            'to_id': to_id,
            'is_typing': is_typing
        }, room=str(to_id))

        emit('typing_ack', {'ok': True})
    except Exception as e:
        emit('error', {'error': str(e)})


@socketio.on('mark_seen')
def handle_mark_seen(payload):
    """
    payload: { other_id: 'the other user id' }
    Marks messages seen in DB and notifies the other user.
    """
    try:
        user_id = session.get('user_id')
        other_id = payload.get('other_id')
        if not user_id or not other_id:
            return emit('error', {'error': 'Missing fields'})

        r = supabase.table('messages') \
            .update({'seen': True}) \
            .eq('receiver_id', user_id) \
            .eq('sender_id', other_id) \
            .eq('seen', False) \
            .execute()

        # notify the other user that messages were seen
        socketio.emit('messages_seen', {
            'by': user_id,
            'other_id': other_id
        }, room=str(other_id))

        emit('mark_seen_ack', {'ok': True, 'updated': len(r.data) if hasattr(r, "data") and r.data else 0})
    except Exception as e:
        emit('error', {'error': str(e)})

# ---------- Small modifications to HTTP endpoints to also emit realtime when used ----------

@app.route("/")
def main_page():
    return render_template('index.html')

@app.route("/policy")
def policy_page():
    return render_template('policy.html')

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("20 per minute")
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        if not email or not password:
            return render_template('log-in.html', error="Please provide both email and password.")

        user_data = supabase.table('users').select('*').eq('email', email).single().execute()
        if not user_data.data:
            return render_template('log-in.html', error="Incorrect Email or Password.")

        user = user_data.data
        if bcrypt.checkpw(password.encode('utf-8'), user['password'].encode('utf-8')):
            session['user_id'] = user['id']
            session['is_verified'] = user.get('is_verified', False)
            session['account_type'] = user.get('account_type', '').strip().lower()

            if session['account_type'] == 'university':
                return redirect('/home') if session['is_verified'] else redirect('/verify')
            elif session['account_type'] == 'student':
                return redirect('/questions')
            return redirect('/home')
        return render_template('log-in.html', error="Incorrect Email or Password.")
    return render_template('log-in.html')

@app.route('/register', methods=['GET', 'POST'])
@limiter.limit("20 per minute")
def register():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        name = request.form['name']
        account_type = request.form['account_type'].strip().lower()
        existing_user = supabase.table('users').select('id').eq('email', email).execute()
        if existing_user.data:
            return render_template('register.html', error="This email is already registered.")

        hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        is_verified = None if account_type == 'student' else False

        supabase.table('users').insert({
            'email': email,
            'password': hashed_password,
            'name': name,
            'account_type': account_type,
            'is_verified': is_verified
        }).execute()

        user_data = supabase.table('users').select('*').eq('email', email).single().execute()
        user = user_data.data
        session['user_id'] = user['id']
        session['is_verified'] = user.get('is_verified', False)
        session['account_type'] = account_type

        return redirect('/questions') if account_type == 'student' else redirect('/verify')
    return render_template('register.html')

@app.route('/verify', methods=['GET', 'POST'])
@login_required
@university_required
def verify_page():
    user_id = session.get('user_id')
    user_data = supabase.table('users').select('name').eq('id', user_id).single().execute()
    name = user_data.data['name'] if user_data.data else "User"

    if request.method == 'POST':
        file = request.files.get('verification_image')
        if not file:
            return render_template('verify.html', name=name, error="No file uploaded.")

        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as temp:
                file.save(temp.name)
                text = pytesseract.image_to_string(Image.open(temp.name))

            # Basic verification logic: check if a known keyword appears
            if any(keyword.lower() in text.lower() for keyword in ["university", "student", "college"]):
                # Mark user as verified in Supabase
                supabase.table('users').update({'is_verified': True}).eq('id', user_id).execute()
                session['is_verified'] = True
                return redirect('/mentor-home')  # Assuming /upload is the next page after successful verification
            else:
                return render_template('verify.html', name=name, error="Verification failed. Try a clearer image.")

        except Exception as e:
            return render_template('verify.html', name=name, error=f"Error during verification: {str(e)}")

    return render_template('verify.html', name=name)

@app.route("/questions", methods=["GET", "POST"])
@quiz_not_taken_required
@highschool_required
def questions():
    if request.method == "POST":
        user_id = session.get("user_id")
        data = request.json
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

@app.route('/home')
@login_required
@highschool_required
def home_page():
    user_id = session.get('user_id')
    user_data = supabase.table('users').select('name').eq('id', user_id).single().execute()
    name = user_data.data['name'] if user_data.data else "User"
    return render_template('home.html', name=name)

@app.route("/courses")
@login_required
@highschool_required
def course_page():
    return render_template('courses.html')

@app.route("/explore")
@login_required
@highschool_required
def explore_page():
    return render_template('explore.html')

@app.route("/mentors")
@login_required
@highschool_required
def mentors_page():
    return render_template('mentors.html')

@app.route("/messages")
@login_required
@highschool_required
def messages_page():
    # Student page. Inject supabase anon key + current user id (student).
    return render_template('messages.html',
                           supabase_url=SUPABASE_URL,
                           supabase_anon_key=SUPABASE_ANON_KEY,
                           my_user_id=session.get('user_id'))

@app.route("/mentor_messages")
@login_required
@university_verified_required
def mentor_messages_page():
    # Mentor page. Inject supabase anon key + current user id (mentor).
    return render_template('mentor_messages.html',
                           supabase_url=SUPABASE_URL,
                           supabase_anon_key=SUPABASE_ANON_KEY,
                           my_user_id=session.get('user_id'))

# ---------- APIs ----------

@app.route("/api/my_mentor")
@login_required
@highschool_required
def api_my_mentor():
    student_id = session.get('user_id')

    # Fetch the mentor-student link
    res = supabase.table('mentor_student_links') \
        .select('mentor_id, approved') \
        .eq('student_id', student_id) \
        .limit(1) \
        .execute()


    # No mentor assigned
    if not res.data or len(res.data) == 0:
        return jsonify({"data": None}), 200

    # Extract mentor details
    rec = res.data[0]
    mentor_id = rec.get('mentor_id')
    approved = rec.get('approved', False)

    if not mentor_id:
        return jsonify({"data": None}), 200

    # Fetch mentor profile
    mentor_res = supabase.table('users') \
        .select('id, name, email, account_type') \
        .eq('id', mentor_id) \
        .single() \
        .execute()

    return jsonify({
        "data": {
            "mentor": mentor_res.data,
            "approved": approved
        }
    }), 200

# For mentors: list students who selected them (optionally filter approved)
@app.route("/api/my_requests")
@login_required
@university_verified_required
def api_my_requests():
    mentor_id = session.get('user_id')

    # Fetch mentor-student links
    res = supabase.table('mentor_student_links') \
        .select('student_id,approved,created_at') \
        .eq('mentor_id', mentor_id) \
        .order('created_at', desc=False) \
        .execute()

    if isinstance(res, dict) and res.get("error"):
        return jsonify({"error": res["error"]["message"]}), 500

    student_ids = [r['student_id'] for r in res.data] if res.data else []
    if not student_ids:
        return jsonify({"data": []})

    # Fetch student profiles
    students = supabase.table('users') \
        .select('id,name,email,account_type') \
        .in_('id', student_ids) \
        .execute()

    if isinstance(students, dict) and students.get("error"):
        return jsonify({"error": students["error"]["message"]}), 500

    # Join data
    out = []
    by_id = {s['id']: s for s in (students.data or [])}
    for rec in res.data:
        sid = rec['student_id']
        out.append({
            "student": by_id.get(sid),
            "approved": rec.get('approved', False),
            "created_at": rec.get('created_at')
        })

    return jsonify({"data": out})


# Mentor approves a student -> set approved = true in mentor_student_links
@app.route("/api/approve_student", methods=["POST"])
@login_required
@university_verified_required
def api_approve_student():
    payload = request.get_json() or {}
    student_id = payload.get('student_id')
    mentor_id = session.get('user_id')

    if not student_id:
        return jsonify({"error": "Missing student_id"}), 400

    # Update approval status
    r = supabase.table('mentor_student_links') \
        .update({'approved': True}) \
        .eq('mentor_id', mentor_id) \
        .eq('student_id', student_id) \
        .execute()

    if isinstance(r, dict) and r.get("error"):
        return jsonify({"error": r["error"]["message"]}), 500

    return jsonify({"ok": True})

# Fetch messages between current user and other_id (only authorized pairs)
@app.route("/api/messages/<other_id>")
@login_required
def api_messages_with(other_id):
    try:
        user_id = session.get('user_id')
        account_type = session.get('account_type')

        # Authorization check
        if account_type == 'student':
            check = supabase.table('mentor_student_links') \
                .select('approved') \
                .eq('student_id', user_id) \
                .eq('mentor_id', other_id) \
                .limit(1) \
                .execute()
            if not check.data or not check.data[0].get('approved', False):
                return jsonify({"error": "Not authorized"}), 403

        elif account_type == 'university':
            check = supabase.table('mentor_student_links') \
                .select('approved') \
                .eq('mentor_id', user_id) \
                .eq('student_id', other_id) \
                .limit(1) \
                .execute()
            if not check.data or not check.data[0].get('approved', False):
                return jsonify({"error": "Not authorized"}), 403

        # Fetch messages both ways
        expr = (
            f"or("
            f"and(sender_id.eq.{user_id},receiver_id.eq.{other_id}),"
            f"and(sender_id.eq.{other_id},receiver_id.eq.{user_id})"
            f")"
        )
        msgs = supabase.table('messages') \
            .select('*') \
            .or_(expr) \
            .order('created_at', desc=False) \
            .execute()

        if isinstance(msgs, dict) and msgs.get("error"):
            return jsonify({"error": msgs["error"]["message"]}), 500

        raw_data = msgs.data if hasattr(msgs, "data") else []

        # Deduplicate based on unique keys (timestamp + sender + receiver + message text)
        seen = set()
        unique_msgs = []
        for m in raw_data:
            key = (
                m.get("created_at"),
                m.get("sender_id"),
                m.get("receiver_id"),
                m.get("message")
            )
            if key not in seen:
                seen.add(key)
                unique_msgs.append(m)

        return jsonify({"data": unique_msgs})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Example: modify your /api/messages/send to emit so existing clients using fetch still get realtime
@app.route("/api/messages/send", methods=["POST"])
@login_required
def api_send_message():
    try:
        payload = request.get_json() or {}
        sender_id = session.get('user_id')
        receiver_id = payload.get('receiver_id')
        message = (payload.get('message') or '').strip()

        if not receiver_id or not message:
            return jsonify({"error": "Missing receiver_id or message"}), 400

        # Authorization checks
        account_type = session.get('account_type')
        if account_type == 'student':
            check = supabase.table('mentor_student_links') \
                .select('approved') \
                .eq('student_id', sender_id) \
                .eq('mentor_id', receiver_id) \
                .limit(1) \
                .execute()
            if not check.data or not check.data[0].get('approved', False):
                return jsonify({"error": "Not authorized to send to this mentor."}), 403

        elif account_type == 'university':
            check = supabase.table('mentor_student_links') \
                .select('approved') \
                .eq('mentor_id', sender_id) \
                .eq('student_id', receiver_id) \
                .limit(1) \
                .execute()
            if not check.data or not check.data[0].get('approved', False):
                return jsonify({"error": "Not authorized to send to this student."}), 403

        # Check for duplicates in the last 4 seconds
        from datetime import datetime, timedelta, timezone
        four_seconds_ago = datetime.now(timezone.utc) - timedelta(seconds=4)

        recent = supabase.table('messages') \
            .select('id, created_at') \
            .eq('sender_id', sender_id) \
            .eq('receiver_id', receiver_id) \
            .order('created_at', desc=True) \
            .limit(1) \
            .execute()

        if recent.data:
            last_msg_time = datetime.fromisoformat(recent.data[0]['created_at'].replace("Z", "+00:00"))
            if last_msg_time >= four_seconds_ago:
                return jsonify({"ok": False, "error": "Duplicate message blocked"}), 200

        # Insert new message
        insert_data = {
            "sender_id": sender_id,
            "receiver_id": receiver_id,
            "message": message
        }
        r = supabase.table('messages').insert(insert_data).execute()
        if isinstance(r, dict) and r.get("error"):
            return jsonify({"error": r["error"]["message"]}), 500

        row = r.data[0] if hasattr(r, "data") and r.data else None

        # emit to rooms
        socketio.emit('new_message', row, room=str(receiver_id))
        socketio.emit('new_message', row, room=str(sender_id))

        return jsonify({"ok": True, "row": row})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/typing", methods=["POST"])
@login_required
def api_typing():
    try:
        payload = request.get_json() or {}
        to_id = payload.get('to_id')
        is_typing = bool(payload.get('is_typing'))
        user_id = session.get('user_id')

        if not to_id:
            return jsonify({"error": "Missing to_id"}), 400

        # DB update (same as previous)
        q = supabase.table('typing_status') \
            .select('id') \
            .eq('from_id', user_id) \
            .eq('to_id', to_id) \
            .limit(1) \
            .execute()

        if hasattr(q, "data") and q.data:
            r = supabase.table('typing_status') \
                .update({
                    'is_typing': is_typing,
                    'updated_at': 'now()'
                }) \
                .eq('from_id', user_id) \
                .eq('to_id', to_id) \
                .execute()
        else:
            r = supabase.table('typing_status') \
                .insert({
                    'from_id': user_id,
                    'to_id': to_id,
                    'is_typing': is_typing
                }) \
                .execute()

        # Emit realtime typing update
        socketio.emit('typing_update', {
            'from_id': user_id,
            'to_id': to_id,
            'is_typing': is_typing
        }, room=str(to_id))

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/messages/mark_seen/<other_id>", methods=['POST'])
@login_required
def api_mark_seen(other_id):
    try:
        user_id = session.get('user_id')
        r = supabase.table('messages') \
            .update({'seen': True}) \
            .eq('receiver_id', user_id) \
            .eq('sender_id', other_id) \
            .eq('seen', False) \
            .execute()

        # emit notification to other user
        socketio.emit('messages_seen', {
            'by': user_id,
            'other_id': other_id
        }, room=str(other_id))

        updated_count = len(r.data) if hasattr(r, "data") and r.data else 0
        return jsonify({"ok": True, "updated": updated_count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Get current user info
@app.route("/api/me")
@login_required
def api_me():
    try:
        user_id = session.get('user_id')
        r = supabase.table('users') \
            .select('id,name,email,account_type') \
            .eq('id', user_id) \
            .single() \
            .execute()

        if isinstance(r, dict) and r.get("error"):
            return jsonify({"error": r["error"]["message"]}), 500

        return jsonify({"data": r.data if hasattr(r, "data") else None})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/mentor_home")
@university_verified_required
@login_required
def mentor_home_page():
    user_id = session.get('user_id')
    user_data = supabase.table('users').select('name').eq('id', user_id).single().execute()
    name = user_data.data['name'] if user_data.data else "User"
    return render_template('mentor_home.html', name=name)

@app.route("/mentees")
@university_verified_required
@login_required
def mentees_page():
    return render_template('mentees.html')


@app.route("/mentor_analytics")
@university_verified_required
@login_required
def mentor_analytics_page():
    return render_template('mentor_analytics.html')



@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=int(os.getenv("PORT", 5000)))
