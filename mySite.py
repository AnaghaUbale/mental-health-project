from flask import *
from werkzeug.security import generate_password_hash, check_password_hash
from flask_mail import Mail, Message
from datetime import datetime, timedelta  
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfgen import canvas
from textblob import TextBlob
from pydub import AudioSegment
from functools import wraps


import base64
import sqlite3
# import razorpay
import io
import os
import pickle
import cv2
import librosa
import numpy as np

# ---------------- APP INIT ----------------
os.environ["SECRET_KEY"] = "my_super_secret_key_123"
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB
app.secret_key = os.environ["SECRET_KEY"]
app.permanent_session_lifetime = timedelta(minutes=30)

# ----------------------------------------
def login_required(role=None):
    def wrapper(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):

            if "user_id" not in session:
                return redirect(url_for("login"))

            if role and session.get("role") != role:
                flash("Unauthorized access!", "danger")
                return redirect(url_for("dashboard"))

            return f(*args, **kwargs)

        return decorated_function
    return wrapper

# ---------------- EMAIL CONFIG ----------------
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv("MAIL_USERNAME")
app.config['MAIL_PASSWORD'] = os.getenv("MAIL_PASSWORD")

mail = Mail(app)

# ---------------- LOAD MODEL ----------------
try:
    model = pickle.load(open("model.pkl", "rb"))
    print("Model loaded successfully")
except Exception as e:
    print("Model not loaded:", e)
    model = None

# ---------------- DATABASE ----------------
def get_db_connection():
    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    # ---------------- USERS ----------------
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT,
        email TEXT UNIQUE,
        password TEXT,
        role TEXT CHECK(role IN ('admin','doctor','patient')) DEFAULT 'patient',
        phone TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # ---------------- DOCTORS ----------------
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS doctors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE,
        specialization TEXT,
        experience INTEGER,
        fees INTEGER,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    # ---------------- TEST RESULTS ----------------
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS test_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        text_score REAL,
        voice_score REAL,
        video_score REAL,
        final_score REAL,
        risk_level TEXT,
        date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    # ---------------- BOOKINGS ----------------
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        doctor_id INTEGER,
        date TEXT,
        time TEXT,
        status TEXT DEFAULT 'Pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (doctor_id) REFERENCES doctors(id)
    )
    """)

    conn.commit()
    conn.close()

# ---------------- TRUNCATE ---------------
def delete():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute("DELETE FROM doctors")
    cursor.execute("DELETE FROM sqlite_sequence WHERE name='doctors'")

    conn.commit()
    conn.close()

# ---------------- EMAIL FUNCTION ----------------
def send_alert_email(user_email, risk_level):
    if risk_level.lower() != "high risk":
        return

    msg = Message(
        subject="⚠️ Mental Health Alert",
        sender=app.config['MAIL_USERNAME'],
        recipients=[user_email]
    )

    msg.body = f"""
Hello,

Our system detected a HIGH risk level based on your recent test.

Please consider:
- Talking to someone you trust
- Consulting a professional

You are not alone 💙

AI Mental Health System
"""
    mail.send(msg)

# ---------------- ROUTES ----------------
# HOME
@app.route("/")
def home():
    return render_template("home.html")

# REGISTER
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":

        full_name = request.form.get("full_name")
        email = request.form.get("email")
        password_raw = request.form.get("password")

        # Basic validation
        if not full_name or not email or not password_raw:
            flash("All fields are required!", "danger")
            return redirect(url_for("register"))

        # Hash password
        password = generate_password_hash(password_raw)

        # 🔐 FORCE ROLE (never trust frontend)
        role = "patient"

        conn = get_db_connection()
        cursor = conn.cursor()

        # Check if email already exists
        existing = cursor.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()

        if existing:
            conn.close()
            flash("Email already exists", "warning")
            return redirect(url_for("login"))

        # Insert user
        cursor.execute("""
            INSERT INTO users (full_name, email, password, role)
            VALUES (?, ?, ?, ?)
        """, (full_name, email, password, role))

        conn.commit()
        conn.close()

        flash("Registered successfully", "success")
        return redirect(url_for("login"))

    return render_template("register.html")

# LOGIN
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        conn = get_db_connection()
        cursor = conn.cursor()

        user = cursor.execute(
            "SELECT * FROM users WHERE email=?", (email,)
        ).fetchone()

        conn.close()

        if user and check_password_hash(user["password"], password):

            session["user_id"] = user["id"]
            session["user_name"] = user["full_name"]
            session["email"] = user["email"]
            session["role"] = user["role"]

            # 🔥 ROLE BASED REDIRECT
            if user["role"] == "admin":
                return redirect(url_for("admin_dashboard"))

            elif user["role"] == "doctor":
                return redirect(url_for("doctor_dashboard"))

            else:
                return redirect(url_for("dashboard"))

        else:
            flash("Invalid credentials", "danger")

    return render_template("login.html")

# PATIENT DASHBOARD
@app.route("/dashboard")
@login_required()
def dashboard():
    # Role-based redirection
    if session["role"] == "doctor":
        return redirect(url_for("doctor_dashboard"))

    # -------- PATIENT DASHBOARD --------
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*) FROM test_results WHERE user_id=?
    """, (session["user_id"],))
    total_tests = cursor.fetchone()[0]

    cursor.execute("""
        SELECT final_score, risk_level 
        FROM test_results 
        WHERE user_id=? 
        ORDER BY date DESC LIMIT 1
    """, (session["user_id"],))

    last = cursor.fetchone()
    conn.close()

    last_score = round(last[0]*100, 2) if last else 0
    last_risk = last[1] if last else "N/A"

    return render_template("dashboard.html",
                           name=session["user_name"],
                           total_tests=total_tests,
                           last_score=last_score,
                           last_risk=last_risk)

# DOCTOR DASHBOARD
@app.route("/doctor_dashboard")
@login_required()
def doctor_dashboard():

    if session.get("role") != "doctor":
        return redirect(url_for("login"))

    conn = get_db_connection()
    cursor = conn.cursor()

    bookings = cursor.execute("""
        SELECT b.id, u.full_name, u.email,
               b.date, b.time,
               b.booking_status, b.payment_status,

               -- Latest test result
               (SELECT final_score 
                FROM test_results tr 
                WHERE tr.user_id = u.id 
                ORDER BY tr.date DESC LIMIT 1) as score,

               (SELECT risk_level 
                FROM test_results tr 
                WHERE tr.user_id = u.id 
                ORDER BY tr.date DESC LIMIT 1) as risk

        FROM bookings b
        JOIN users u ON b.user_id = u.id
        JOIN doctors d ON b.doctor_id = d.id
        WHERE d.user_id = ?
        ORDER BY b.date DESC

    """, (session["user_id"],)).fetchall()

    conn.close()

    return render_template("doctor_dashboard.html", bookings=bookings)

# ADMIN DASHBOARD
@app.route("/admin_dashboard")
@login_required()
def admin_dashboard():

    if session.get("role") != "admin":
        return redirect(url_for("login"))

    conn = get_db_connection()
    cursor = conn.cursor()

    # Total users
    total_users = cursor.execute(
        "SELECT COUNT(*) FROM users WHERE role='patient'"
    ).fetchone()[0]

    # Total doctors
    total_doctors = cursor.execute(
        "SELECT COUNT(*) FROM doctors"
    ).fetchone()[0]

    # Total bookings
    total_bookings = cursor.execute(
        "SELECT COUNT(*) FROM bookings"
    ).fetchone()[0]

    # Get doctors list
    doctors = cursor.execute("""
        SELECT u.id, u.full_name, u.email,
               d.specialization, d.experience, d.fees
        FROM users u
        JOIN doctors d ON u.id = d.user_id
    """).fetchall()

    conn.close()

    return render_template("admin_dashboard.html",
                           total_users=total_users,
                           total_doctors=total_doctors,
                           total_bookings=total_bookings,
                           doctors=doctors)

# ADD DOCTOR
@app.route("/add-doctor", methods=["GET", "POST"])
@login_required()
def add_doctor():

    # Only admin allowed
    if session.get("role") != "admin":
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        full_name = request.form["full_name"]
        email = request.form["email"]
        password = generate_password_hash(request.form["password"])
        specialization = request.form["specialization"]
        experience = request.form["experience"]
        fees = request.form["fees"]

        conn = get_db_connection()
        cursor = conn.cursor()

        # Check email already exists
        existing = cursor.execute(
            "SELECT * FROM users WHERE email=?", (email,)
        ).fetchone()

        if existing:
            flash("Doctor email already exists!", "danger")
            return redirect(url_for("add_doctor"))

        # 1️⃣ Insert into USERS table
        cursor.execute("""
            INSERT INTO users (full_name, email, password, role)
            VALUES (?, ?, ?, 'doctor')
        """, (full_name, email, password))

        user_id = cursor.lastrowid

        # 2️⃣ Insert into DOCTORS table
        cursor.execute("""
            INSERT INTO doctors (user_id, specialization, experience, fees)
            VALUES (?, ?, ?, ?)
        """, (user_id, specialization, experience, fees))

        conn.commit()
        conn.close()

        flash("Doctor added successfully!", "success")
        return redirect(url_for("admin_dashboard"))

    return render_template("add_doctor.html")

# EDIT DOCTOR
@app.route("/edit-doctor/<int:id>", methods=["GET", "POST"])
@login_required()
def edit_doctor(id):

    if session.get("role") != "admin":
        return redirect(url_for("login"))

    conn = get_db_connection()
    cursor = conn.cursor()

    # GET existing doctor data
    doctor = cursor.execute("""
        SELECT u.id, u.full_name, u.email,
               d.specialization, d.experience, d.fees
        FROM users u
        JOIN doctors d ON u.id = d.user_id
        WHERE u.id = ?
    """, (id,)).fetchone()

    if request.method == "POST":
        name = request.form["full_name"]
        email = request.form["email"]
        specialization = request.form["specialization"]
        experience = request.form["experience"]
        fees = request.form["fees"]

        # UPDATE users table
        cursor.execute("""
            UPDATE users
            SET full_name = ?, email = ?
            WHERE id = ?
        """, (name, email, id))

        # UPDATE doctors table
        cursor.execute("""
            UPDATE doctors
            SET specialization = ?, experience = ?, fees = ?
            WHERE user_id = ?
        """, (specialization, experience, fees, id))

        conn.commit()
        conn.close()

        flash("Doctor updated successfully", "success")
        return redirect(url_for("admin_dashboard"))

    conn.close()

    return render_template("edit_doctor.html", doctor=doctor)

# DELETE DOCTOR
@app.route("/delete-doctor/<int:id>")
@login_required()
def delete_doctor(id):

    if session.get("role") != "admin":
        return redirect(url_for("login"))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 1. Delete from bookings (important to avoid foreign key issues)
        cursor.execute("DELETE FROM bookings WHERE doctor_id = ?", (id,))

        # 2. Delete from doctors table
        cursor.execute("DELETE FROM doctors WHERE user_id = ?", (id,))

        # 3. Delete from users table
        cursor.execute("DELETE FROM users WHERE id = ?", (id,))

        conn.commit()

        flash("Doctor deleted successfully", "success")

    except Exception as e:
        print("Delete error:", e)
        flash("Error deleting doctor", "danger")

    finally:
        conn.close()

    return redirect(url_for("admin_dashboard"))

# ACCEPT BOOKING
@app.route("/accept-booking/<int:id>")
@login_required()
def accept_booking(id):

    if session.get("role") != "doctor":
        return redirect(url_for("login"))

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE bookings
        SET booking_status = 'Accepted'
        WHERE id = ?
    """, (id,))

    conn.commit()
    conn.close()

    flash("Booking accepted", "success")
    return redirect(url_for("doctor_dashboard"))

# REJECT BOOKING
@app.route("/reject-booking/<int:id>")
@login_required()
def reject_booking(id):

    if session.get("role") != "doctor":
        return redirect(url_for("login"))

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE bookings
        SET booking_status = 'Rejected'
        WHERE id = ?
    """, (id,))

    conn.commit()
    conn.close()

    flash("Booking rejected", "danger")
    return redirect(url_for("doctor_dashboard"))

# START TEST
@app.route("/start_test")
def start_test():

    # Check login
    if "user_id" not in session:
        return redirect(url_for("login"))

    # Reset previous test data
    session.pop("text_result", None)
    session.pop("voice_result", None)
    session.pop("video_result", None)
    session.pop("result_saved", None)

    # Redirect to first step
    return redirect(url_for("textmining"))


# TEXT ANALYSIS
@app.route("/textmining", methods=["GET","POST"])
def textmining():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        text = request.form["user_text"]

        try:
            if model:
                score = float(model.predict([text])[0])
            else:
                # fallback if model not loaded
                sentiment = TextBlob(text).sentiment.polarity
                score = (1 - sentiment) / 2
        except Exception as e:
            print("Model prediction error:", e)
            score = 0.5

        session["text_result"] = score
        return redirect(url_for("voice_analysis"))

    return render_template("textmining.html")


# VOICE ANALYSIS
@app.route("/voice_analysis", methods=["GET","POST"])
def voice_analysis():

    if "text_result" not in session:
        return redirect(url_for("textmining"))

    if request.method == "POST":

        audio_data = request.form.get("audio_data")

        if not audio_data:
            return redirect(url_for("voice_analysis"))

        try:
            data = audio_data.split(",")[1]
            audio_bytes = base64.b64decode(data)

            os.makedirs("static/uploads", exist_ok=True)

            webm = f"static/uploads/audio_{session['user_id']}.webm"
            wav = f"static/uploads/audio_{session['user_id']}.wav"

            with open(webm, "wb") as f:
                f.write(audio_bytes)

            AudioSegment.from_file(webm).export(wav, format="wav")

            y, sr = librosa.load(wav, sr=None)

            energy = np.mean(librosa.feature.rms(y=y))
            tempo, _ = librosa.beat.beat_track(y=y, sr=sr)

            if energy < 0.02 and tempo < 100:
                score = 0.8
            elif energy < 0.04:
                score = 0.5
            else:
                score = 0.2

        except Exception as e:
            print("Voice processing error:", e)
            score = 0.5   # fallback

        session["voice_result"] = score

        return redirect(url_for("video_analysis"))

    return render_template("voice.html")

# VIDEO ANALYSIS
@app.route("/video_analysis", methods=["GET", "POST"])
def video_analysis():

    # Check login
    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":

        try:
            # Get base64 video from frontend
            video_data = request.form.get("video_data")

            if not video_data:
                return "No video data received", 400

            video_data = video_data.split(",")[1]
            video_bytes = base64.b64decode(video_data)

            # Save file
            os.makedirs("static/uploads", exist_ok=True)
            video_path = "static/uploads/video.webm"

            with open(video_path, "wb") as f:
                f.write(video_bytes)

            # Process video
            cap = cv2.VideoCapture(video_path)

            frame_count = 0
            sad_frames = 0

            while True:
                ret, frame = cap.read()
                if not ret:
                    break

                frame_count += 1

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

                # simple brightness heuristic
                if gray.mean() < 90:
                    sad_frames += 1

                if frame_count > 60:
                    break

            cap.release()

            score = sad_frames / frame_count if frame_count else 0

            session["video_result"] = score

            return redirect(url_for("results"))

        except Exception as e:
            import traceback
            traceback.print_exc()
            return str(e)

    return render_template("video.html")

# RESULTS
@app.route("/results")
def results():

    # Prevent skipping step
    if "video_result" not in session:
        return redirect(url_for("start_test"))

    text = session.get("text_result", 0)
    voice = session.get("voice_result", 0)
    video = session.get("video_result", 0)

    total = (text + voice + video) / 3

    if total >= 0.7:
        risk = "High Risk"
        alert = "danger"
        advice = "Consult a professional."

    elif total >= 0.4:
        risk = "Moderate Risk"
        alert = "warning"
        advice = "Take care and monitor emotions."

    else:
        risk = "Low Risk"
        alert = "success"
        advice = "You are doing well."

    session["last_risk"] = risk

    # SAVE RESULT ONLY ONCE
    if not session.get("result_saved"):

        conn = sqlite3.connect("database.db")

        conn.execute("""
            INSERT INTO test_results
            (user_id, text_score, voice_score, video_score, final_score, risk_level)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (session["user_id"], text, voice, video, total, risk))

        conn.commit()
        conn.close()

        session["result_saved"] = True

    return render_template("result.html",
                           final_result=risk,
                           advice=advice,
                           alert_type=alert,
                           score=round(total * 100, 2))

# RESET TEST 
@app.route("/reset-test")
def reset_test():

    session.pop("text_result", None)
    session.pop("voice_result", None)
    session.pop("video_result", None)
    session.pop("result_saved", None)

    return redirect(url_for("dashboard"))

# DOWNLOAD REPORT 
@app.route("/download-report")
@login_required()
def download_report():

    buffer = io.BytesIO()

    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()

    story = []

    # Title
    story.append(Paragraph("NeuroSense Test Report", styles['Title']))
    story.append(Spacer(1, 20))

    # User Info
    story.append(Paragraph(f"Name: {session.get('user_name')}", styles['Normal']))
    story.append(Spacer(1, 10))

    # Scores
    text = session.get("text_result", 0)
    voice = session.get("voice_result", 0)
    video = session.get("video_result", 0)
    final = (text + voice + video) / 3

    story.append(Paragraph(f"Text Score: {round(text*100,2)}%", styles['Normal']))
    story.append(Paragraph(f"Voice Score: {round(voice*100,2)}%", styles['Normal']))
    story.append(Paragraph(f"Video Score: {round(video*100,2)}%", styles['Normal']))
    story.append(Spacer(1, 10))

    story.append(Paragraph(f"Final Score: {round(final*100,2)}%", styles['Normal']))

    # Risk
    if final >= 0.7:
        risk = "High Risk"
        advice = "Consult a professional immediately."
    elif final >= 0.4:
        risk = "Moderate Risk"
        advice = "Take care and monitor your mental health."
    else:
        risk = "Low Risk"
        advice = "You are doing well."

    story.append(Paragraph(f"Risk Level: {risk}", styles['Normal']))
    story.append(Spacer(1, 10))
    story.append(Paragraph(f"Advice: {advice}", styles['Normal']))

    doc.build(story)

    buffer.seek(0)

    return send_file(buffer,
                     as_attachment=True,
                     download_name="NeuroSense_Report.pdf",
                     mimetype='application/pdf')

# DOCTOR DOWNLOAD REPORT
@app.route("/doctor-download-report/<int:booking_id>")
@login_required()
def doctor_download_report(booking_id):

    conn = get_db_connection()
    cursor = conn.cursor()

    # Get patient + test data
    data = cursor.execute("""
        SELECT u.full_name, u.email,
               tr.text_score, tr.voice_score, tr.video_score,
               tr.final_score, tr.risk_level
        FROM bookings b
        JOIN users u ON b.user_id = u.id
        LEFT JOIN test_results tr ON tr.user_id = u.id
        WHERE b.id = ?
        ORDER BY tr.date DESC LIMIT 1
    """, (booking_id,)).fetchone()

    conn.close()

    if not data:
        return "No report found"

    import io
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()

    story = []

    # Title
    story.append(Paragraph("Patient Mental Health Report", styles['Title']))
    story.append(Spacer(1, 20))

    # Patient Info
    story.append(Paragraph(f"Name: {data[0]}", styles['Normal']))
    story.append(Paragraph(f"Email: {data[1]}", styles['Normal']))
    story.append(Spacer(1, 10))

    # Scores
    story.append(Paragraph(f"Text Score: {round(data[2]*100,2)}%", styles['Normal']))
    story.append(Paragraph(f"Voice Score: {round(data[3]*100,2)}%", styles['Normal']))
    story.append(Paragraph(f"Video Score: {round(data[4]*100,2)}%", styles['Normal']))
    story.append(Spacer(1, 10))

    story.append(Paragraph(f"Final Score: {round(data[5]*100,2)}%", styles['Normal']))
    story.append(Spacer(1, 10))

    story.append(Paragraph(f"Risk Level: {data[6]}", styles['Normal']))

    doc.build(story)

    buffer.seek(0)

    return send_file(buffer,
                     as_attachment=True,
                     download_name="Patient_Report.pdf",
                     mimetype='application/pdf')

# HISTORY
@app.route("/history")
@login_required()
def history():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT text_score, voice_score, video_score,
               final_score, risk_level, date
        FROM test_results
        WHERE user_id = ?
        ORDER BY date DESC
    """, (session["user_id"],))

    records = cursor.fetchall()
    conn.close()

    return render_template("history.html", history=records)

# ANALYTICS PAGE
@app.route("/analytics")
@login_required()
def analytics():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT date, final_score
        FROM test_results
        WHERE user_id = ?
        ORDER BY date
    """, (session["user_id"],))

    data = cursor.fetchall()
    conn.close()

    dates = [row[0] for row in data]
    scores = [round(row[1]*100, 2) for row in data]

    return render_template("analytics.html",
                           dates=dates,
                           scores=scores)

# RECOMENDATION
@app.route("/recommendations")
@login_required()
def recommendations():
    risk = session.get("last_risk", "Low Risk")

    # Default data
    books = []
    songs = []
    videos = []

    if risk == "High Risk":
        books = ["The Depression Cure", "Feeling Good by David Burns"]
        songs = ["Weightless - Marconi Union", "Fix You - Coldplay"]
        videos = ["Guided Meditation for Anxiety", "Overcoming Depression"]

    elif risk == "Moderate Risk":
        books = ["Atomic Habits", "The Power of Now"]
        songs = ["Let Her Go - Passenger", "Someone Like You - Adele"]
        videos = ["Motivation for Hard Times", "Stress Management Tips"]

    else:
        books = ["The Alchemist", "Ikigai"]
        songs = ["Happy - Pharrell Williams", "On Top of the World"]
        videos = ["Daily Motivation", "Positive Thinking"]

    return render_template("recommendations.html",
                           risk=risk,
                           books=books,
                           songs=songs,
                           videos=videos)

# DOCTOR
@app.route("/doctors")
@login_required()
def doctors():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT doctors.id, users.full_name, doctors.specialization, doctors.fees
        FROM doctors
        JOIN users ON doctors.user_id = users.id
    """)

    doctors = cursor.fetchall()
    conn.close()

    return render_template("doctors.html", doctors=doctors)

# APPOINTMENT
@app.route("/book/<int:doctor_id>", methods=["GET", "POST"])
@login_required()
def book(doctor_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    # Fetch doctor details
    cursor.execute("""
        SELECT doctors.id, users.full_name, doctors.specialization, doctors.fees
        FROM doctors
        JOIN users ON doctors.user_id = users.id
        WHERE doctors.id = ?
    """, (doctor_id,))

    doctor = cursor.fetchone()

    if request.method == "POST":
        date = request.form["date"]
        time = request.form["time"]

        # Insert booking
        cursor.execute("""
            INSERT INTO bookings (user_id, doctor_id, date, time, payment_status, booking_status)
            VALUES (?, ?, ?, ?, 'Pending', 'Pending')
        """, (session["user_id"], doctor_id, date, time))

        # Assign doctor (avoid duplicate)
        cursor.execute("""
            INSERT OR IGNORE INTO assignments (patient_id, doctor_id)
            VALUES (?, ?)
        """, (session["user_id"], doctor_id))

        conn.commit()
        conn.close()

        return redirect(url_for("booking_history"))

    conn.close()
    return render_template("book.html", doctor=doctor)

# PATIENTS
@app.route("/patient/<int:patient_id>")
@login_required()
def patient_details(patient_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    # Secure patient access
    cursor.execute("""
        SELECT users.full_name, users.email
        FROM assignments
        JOIN users ON assignments.patient_id = users.id
        WHERE assignments.doctor_id = ? AND users.id = ?
    """, (session["user_id"], patient_id))

    patient = cursor.fetchone()

    if not patient:
        conn.close()
        flash("Unauthorized access!", "danger")
        return redirect(url_for("doctor_dashboard"))

    # Test results (same as before)
    cursor.execute("""
        SELECT final_score, risk_level, date
        FROM test_results
        WHERE user_id = ?
        ORDER BY date DESC
    """, (patient_id,))

    reports = cursor.fetchall()
    conn.close()

    return render_template("patient_details.html",
                           patient=patient,
                           reports=reports)

# PAYMENT
@app.route("/pay/<int:booking_id>")
@login_required()
def payment(booking_id):
    return render_template("payment.html", booking_id=booking_id)

# PAYMENT SUCCESS
@app.route("/payment-success/<int:booking_id>")
@login_required()
def payment_success(booking_id):

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE bookings
        SET payment_status = 'Paid'
        WHERE id = ?
    """, (booking_id,))

    conn.commit()
    conn.close()

    return redirect(url_for("booking_history"))

# BOOKING HISTORY
@app.route("/booking-history")
@login_required()
def booking_history():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT 
            d.user_id AS doctor_id,
            u.full_name AS doctor_name,
            b.date,
            b.time,
            b.payment_status,
            b.booking_status,
            b.id AS booking_id
        FROM bookings b
        JOIN doctors d ON b.doctor_id = d.id
        JOIN users u ON d.user_id = u.id
        WHERE b.user_id = ?
        ORDER BY b.created_at DESC
    """, (session["user_id"],))

    bookings = cursor.fetchall()
    conn.close()

    return render_template("booking_history.html", bookings=bookings)

# CHATBOT
@app.route("/chatbot")
@login_required()
def chatbot():
    return render_template("chatbot.html")

@app.route("/chat", methods=["POST"])
def chat():

    user_msg = request.json.get("message", "")

    # Analyze sentiment
    sentiment = TextBlob(user_msg).sentiment.polarity

    # Get user's last risk (if available)
    risk = session.get("last_risk", "Low Risk")

    # Generate response based on sentiment
    if sentiment < -0.5:
        reply = "I'm really sorry you're feeling this way 💙. You are not alone. Talking to someone you trust or a professional can really help."

    elif sentiment < 0:
        reply = "I understand things might be tough right now. Try taking a short break, breathing deeply, or doing something you enjoy."

    elif sentiment > 0.5:
        reply = "That's wonderful to hear 😊 Keep doing what makes you happy and stay positive!"

    else:
        reply = "I'm here for you. You can share anything or explore tests and recommendations."

    # Add suggestion based on risk
    if risk == "High Risk":
        reply += " Also, I strongly recommend booking a session with a doctor."

    elif risk == "Moderate Risk":
        reply += " You may also check out some recommendations on your dashboard."

    return jsonify({"reply": reply})

# LOGOUT
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# RUN
if __name__=="__main__":
    print("Starting NeuroSense Application...")
    init_db()
    print("Database created successfully")
    app.run(debug=True)
