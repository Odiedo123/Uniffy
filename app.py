from flask import Flask, request, send_from_directory, jsonify, render_template, redirect, url_for, session, Response, make_response, send_file
from supabase import create_client, Client
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from functools import wraps
from datetime import datetime
from dotenv import load_dotenv
from PIL import Image
import pytesseract
import tempfile
import os
import json
import bcrypt

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

limiter = Limiter(
    key_func=lambda: session.get("user_id", get_remote_address()),
    app=app,
    default_limits=["100 per minute"]
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
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
            return redirect(request.referrer or url_for('mentor_home_page'))

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

@app.route("/messages")
@login_required
@highschool_required
def message_page():
    return render_template('messages.html')

@app.route("/mentors")
@login_required
@highschool_required
def mentors_page():
    return render_template('mentors.html')

@app.route("/mentor-home")
@login_required
def mentor_home_page():
    return render_template('mentor-home.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)
