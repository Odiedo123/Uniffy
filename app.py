from flask import Flask, request, send_from_directory, jsonify, render_template, redirect, url_for, session, Response, make_response, send_file
from supabase import create_client, Client
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from functools import wraps
from datetime import datetime
from dotenv import load_dotenv
import os
import json
import bcrypt

load_dotenv() 

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

limiter = Limiter(
    key_func=lambda: session.get("user_id", get_remote_address()),  #! Use user_id if logged in, else IP
    app=app,
    default_limits=["100 per minute"]  # !Global limit
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

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

        # Allow only verified university users
        if account_type != 'university' or is_verified is not True:
            # Redirect to previous page or fallback to home
            return redirect(request.referrer or '/')
        
        return f(*args, **kwargs)
    return decorated_function

def quiz_not_taken_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user_id = session.get('user_id')
        account_type = session.get('account_type')

        #! Only students are restricted here
        if not user_id or account_type != 'student':
            return redirect(url_for('login'))

        # !Check if the user already has a quiz result
        result = supabase.table('quiz_results').select('id').eq('user_id', user_id).limit(1).execute()

        if result.data:
            #! Redirect if they already took the quiz
            return redirect(url_for('home_page'))

        return f(*args, **kwargs)
    return decorated_function


@app.route("/")
def main_page():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("20 per minute")
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')

        if not email or not password:
            error = "Please provide both email and password."
            return render_template('log-in.html', error=error)

        user_data = supabase.table('users').select('*').eq('email', email).execute()

        if not user_data.data:
            error = "Incorrect Email or Password."
            return render_template('log-in.html', error=error)

        user = user_data.data[0]

        if bcrypt.checkpw(password.encode('utf-8'), user['password'].encode('utf-8')):
            session['user_id'] = user['id']
            session['is_verified'] = user.get('is_verified', False)
            session['account_type'] = user.get('account_type', '').strip().lower()

            if session['account_type'] == 'university':
                if session['is_verified']:
                    return redirect('/home')
                else:
                    return redirect('/verify')
            elif session['account_type'] == 'student':
                return redirect('/questions')
            else:
                return redirect('/home')  # fallback
        else:
            error = "Incorrect Email or Password."
            return render_template('log-in.html', error=error)

    return render_template('log-in.html')


@app.route('/register', methods=['GET', 'POST'])
@limiter.limit("20 per minute")
def register():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        name = request.form['name']
        account_type = request.form['account_type'].strip().lower()  # 'student' or 'university'

        #! Check if email already exists
        existing_user = supabase.table('users').select('*').eq('email', email).execute()
        if existing_user.data:
            error = "This email is already registered."
            return render_template('register.html', error=error)

        #! Hash the password using bcrypt
        hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

        #! Set verification status
        is_verified = None if account_type == 'student' else False

        #! Insert the new user
        supabase.table('users').insert({
            'email': email,
            'password': hashed_password,
            'name': name,
            'account_type': account_type,
            'is_verified': is_verified
        }).execute()

        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/verify')
@login_required
def verify_page():
    user_id = session.get('user_id')

    user_data = supabase.table('users').select('name').eq('id', user_id).execute()

    if user_data.data:
        name = user_data.data[0]['name']
    else:
        name = "User"

    return render_template('verify.html', name=name)


@app.route("/questions", methods=["GET", "POST"])
@quiz_not_taken_required
def questions():
    if request.method == "POST":
        #! Ensure user is logged in and has a session
        user_id = session.get("user_id")
        if not user_id:
            return redirect("/login")

        data = request.json
        result = data.get("result")  #! main interest, e.g., 'tech'
        interest_scores = data.get("scores")  #! full breakdown as dict

        #! Basic validation
        if not result or not isinstance(interest_scores, dict):
            return {"error": "Invalid quiz data"}, 400

        #! Save quiz result to Supabase
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
def home_page():
    user_id = session.get('user_id')

    user_data = supabase.table('users').select('name').eq('id', user_id).execute()

    if user_data.data:
        name = user_data.data[0]['name']
    else:
        name = "User"

    return render_template('home.html', name=name)


@app.route("/courses")
@login_required
def course_page():
    return render_template('courses.html')

@app.route("/explore")
@login_required
def explore_page():
    return render_template('explore.html')

@app.route("/messages")
@login_required
def message_page():
    return render_template('messages.html')

@app.route("/mentors")
@login_required
def mentors_page():
    return render_template('mentors.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


if __name__ == '__main__':
    app.run(debug=True)