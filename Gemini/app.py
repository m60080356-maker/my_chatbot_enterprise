from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from flask_session import Session
import google.generativeai as genai
import time
import os
import re
import bcrypt
from datetime import datetime
from werkzeug.utils import secure_filename
import PyPDF2
import docx
import logging
import sqlite3

# ==========================================
# 🔑 تنظیمات اولیه
# ==========================================

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your-super-secret-key-change-this-in-production-12345!')
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_PERMANENT'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = 86400

Session(app)

# ==========================================
# 📁 تنظیمات آپلود
# ==========================================

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'docx', 'py', 'js', 'html', 'css', 'json', 'csv', 'md', 'xml', 'yaml'}
MAX_FILE_SIZE = 20 * 1024 * 1024

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs('logs', exist_ok=True)

# ==========================================
# 📝 تنظیمات لاگینگ
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/chatbot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==========================================
# 📦 دیتابیس
# ==========================================

def init_db():
    """ایجاد جداول دیتابیس"""
    conn = sqlite3.connect('chatbot.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            email TEXT,
            model_preference TEXT DEFAULT 'flash',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            response TEXT NOT NULL,
            model_used TEXT,
            response_time REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            file_size INTEGER,
            file_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            details TEXT,
            ip_address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()

# اجرای دیتابیس
init_db()

# ==========================================
# 🧠 راه‌اندازی Gemini
# ==========================================

API_KEY = os.environ.get('API_KEY', 'AIza...')  # ← کلید را از محیط دریافت کن
genai.configure(api_key=API_KEY)
AVAILABLE_MODELS = {
    'flash': 'gemini-2.5-flash',
    'pro': 'gemini-2.5-pro'
}

# ==========================================
# 📄 توابع کمکی
# ==========================================

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def read_file_content(filepath, filename):
    ext = filename.rsplit('.', 1)[1].lower()
    
    try:
        if ext in ['txt', 'py', 'js', 'html', 'css', 'json', 'csv', 'md', 'xml', 'yaml']:
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read()
        elif ext == 'pdf':
            text = ""
            with open(filepath, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                for page in reader.pages:
                    text += page.extract_text() + "\n"
            return text
        elif ext == 'docx':
            doc = docx.Document(filepath)
            return "\n".join([para.text for para in doc.paragraphs])
        else:
            return None
    except Exception as e:
        logger.error(f"خطا در خواندن فایل {filename}: {e}")
        return f"خطا در خواندن فایل: {e}"

def get_file_size(filepath):
    return os.path.getsize(filepath)

def get_model_for_user(user_id):
    conn = sqlite3.connect('chatbot.db')
    cursor = conn.cursor()
    cursor.execute('SELECT model_preference FROM users WHERE id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result and result[0] in AVAILABLE_MODELS:
        return AVAILABLE_MODELS[result[0]]
    return AVAILABLE_MODELS['flash']

def log_activity(user_id, action, details=None, ip=None):
    conn = sqlite3.connect('chatbot.db')
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO logs (user_id, action, details, ip_address) VALUES (?, ?, ?, ?)',
        (user_id, action, details, ip)
    )
    conn.commit()
    conn.close()

def save_history(user_id, message, response, model_used, response_time):
    conn = sqlite3.connect('chatbot.db')
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO history (user_id, message, response, model_used, response_time) VALUES (?, ?, ?, ?, ?)',
        (user_id, message, response, model_used, response_time)
    )
    conn.commit()
    conn.close()

def get_history(user_id, limit=50):
    conn = sqlite3.connect('chatbot.db')
    cursor = conn.cursor()
    cursor.execute(
        'SELECT message, response, model_used, response_time, created_at FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?',
        (user_id, limit)
    )
    results = cursor.fetchall()
    conn.close()
    return list(reversed(results))

# ==========================================
# 🌐 روت‌های اصلی
# ==========================================

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    history = get_history(session['user_id'], 50)
    
    conn = sqlite3.connect('chatbot.db')
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM history WHERE user_id = ?', (session['user_id'],))
    total_msgs = cursor.fetchone()[0]
    conn.close()
    
    return render_template('index.html', 
                         username=session.get('username'),
                         history=history,
                         total_msgs=total_msgs)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        if not username or not password:
            flash('لطفاً نام کاربری و رمز عبور را وارد کنید.', 'error')
            return render_template('login.html')
        
        conn = sqlite3.connect('chatbot.db')
        cursor = conn.cursor()
        cursor.execute('SELECT id, password_hash FROM users WHERE username = ?', (username,))
        result = cursor.fetchone()
        conn.close()
        
        if not result:
            flash('نام کاربری یا رمز عبور اشتباه است.', 'error')
            return render_template('login.html')
        
        try:
            if bcrypt.checkpw(password.encode('utf-8'), result[1].encode('utf-8')):
                session['user_id'] = result[0]
                session['username'] = username
                
                conn = sqlite3.connect('chatbot.db')
                cursor = conn.cursor()
                cursor.execute('UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?', (result[0],))
                conn.commit()
                conn.close()
                
                log_activity(result[0], 'login', 'ورود موفق', request.remote_addr)
                flash('خوش آمدید!', 'success')
                return redirect(url_for('index'))
            else:
                flash('نام کاربری یا رمز عبور اشتباه است.', 'error')
        except:
            flash('خطا در ورود. لطفاً دوباره تلاش کنید.', 'error')
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        email = request.form.get('email', '').strip()
        
        if not username or len(username) < 3:
            flash('نام کاربری باید حداقل ۳ کاراکتر باشد.', 'error')
            return render_template('register.html')
        
        if not password or len(password) < 6:
            flash('رمز عبور باید حداقل ۶ کاراکتر باشد.', 'error')
            return render_template('register.html')
        
        if email and not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
            flash('ایمیل نامعتبر است.', 'error')
            return render_template('register.html')
        
        hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
        
        conn = sqlite3.connect('chatbot.db')
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                'INSERT INTO users (username, password_hash, email) VALUES (?, ?, ?)',
                (username, hashed.decode('utf-8'), email)
            )
            conn.commit()
            flash('ثبت نام با موفقیت انجام شد! لطفاً وارد شوید.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('این نام کاربری قبلاً ثبت شده است.', 'error')
        finally:
            conn.close()
    
    return render_template('register.html')

@app.route('/logout')
def logout():
    user_id = session.get('user_id')
    if user_id:
        log_activity(user_id, 'logout', 'خروج از سیستم', request.remote_addr)
    session.clear()
    flash('از سیستم خارج شدید.', 'info')
    return redirect(url_for('login'))

@app.route('/profile')
def profile():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = sqlite3.connect('chatbot.db')
    cursor = conn.cursor()
    cursor.execute('SELECT username, email, model_preference, created_at, last_login FROM users WHERE id = ?', (session['user_id'],))
    user_data = cursor.fetchone()
    conn.close()
    
    conn = sqlite3.connect('chatbot.db')
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM history WHERE user_id = ?', (session['user_id'],))
    total_msgs = cursor.fetchone()[0]
    conn.close()
    
    return render_template('profile.html', 
                         user=user_data, 
                         total_msgs=total_msgs,
                         models=AVAILABLE_MODELS)

@app.route('/update_model', methods=['POST'])
def update_model():
    if 'user_id' not in session:
        return jsonify({'error': 'ورود لازم است'}), 401
    
    model = request.json.get('model')
    if model not in AVAILABLE_MODELS:
        return jsonify({'error': 'مدل نامعتبر'}), 400
    
    conn = sqlite3.connect('chatbot.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET model_preference = ? WHERE id = ?', (model, session['user_id']))
    conn.commit()
    conn.close()
    
    log_activity(session['user_id'], 'change_model', f'تغییر مدل به {model}', request.remote_addr)
    return jsonify({'success': True})

# ==========================================
# 📨 API چت
# ==========================================

@app.route('/ask', methods=['POST'])
def ask():
    if 'user_id' not in session:
        return jsonify({'error': 'ورود لازم است'}), 401
    
    data = request.get_json()
    user_message = data.get('message', '').strip()
    file_content = data.get('file_content', '').strip()
    
    if not user_message and not file_content:
        return jsonify({'error': 'پیامی وارد نشده است!'}), 400
    
    history = get_history(session['user_id'], 20)
    
    history_text = ""
    for item in history:
        history_text += f"کاربر: {item[0]}\n"
        history_text += f"پاسخ: {item[1]}\n\n"
    
    if file_content:
        full_prompt = f"""محتویات فایل آپلود شده:
----------------------------------------
{file_content}
----------------------------------------

تاریخچه مکالمه:
{history_text}

سوال جدید کاربر: {user_message}

لطفاً بر اساس محتوای فایل و تاریخچه به سوال پاسخ دهید."""
    else:
        full_prompt = f"""تاریخچه مکالمه:
{history_text}

سوال جدید کاربر: {user_message}

لطفاً بر اساس تاریخچه به سوال پاسخ دهید."""
    
    model_name = get_model_for_user(session['user_id'])
    model = genai.GenerativeModel(model_name)
    
    try:
        start_time = time.time()
        response = model.generate_content(full_prompt)
        elapsed_time = time.time() - start_time
        
        reply = response.text
        
        save_history(session['user_id'], user_message, reply, model_name, elapsed_time)
        log_activity(session['user_id'], 'ask', f'سوال: {user_message[:50]}...', request.remote_addr)
        
        return jsonify({
            'reply': reply,
            'time': f'{elapsed_time:.2f}',
            'model': model_name
        })
        
    except Exception as e:
        logger.error(f"خطا در پردازش سوال: {e}")
        return jsonify({'error': f'خطا: {str(e)}'}), 500

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'user_id' not in session:
        return jsonify({'error': 'ورود لازم است'}), 401
    
    if 'file' not in request.files:
        return jsonify({'error': 'هیچ فایلی انتخاب نشده است'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'نام فایل خالی است'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'error': f'نوع فایل مجاز نیست. انواع مجاز: {", ".join(ALLOWED_EXTENSIONS)}'}), 400
    
    try:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        content = read_file_content(filepath, filename)
        size = get_file_size(filepath)
        
        conn = sqlite3.connect('chatbot.db')
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO uploads (user_id, filename, file_size, file_type) VALUES (?, ?, ?, ?)',
            (session['user_id'], filename, size, filename.rsplit('.', 1)[1].lower())
        )
        conn.commit()
        conn.close()
        
        log_activity(session['user_id'], 'upload', f'آپلود فایل: {filename}', request.remote_addr)
        
        os.remove(filepath)
        
        return jsonify({
            'success': True,
            'filename': filename,
            'size': size,
            'content': content[:10000] if content else None
        })
        
    except Exception as e:
        logger.error(f"خطا در آپلود فایل: {e}")
        return jsonify({'error': f'خطا: {str(e)}'}), 500

@app.route('/clear_history', methods=['POST'])
def clear_history():
    if 'user_id' not in session:
        return jsonify({'error': 'ورود لازم است'}), 401
    
    conn = sqlite3.connect('chatbot.db')
    cursor = conn.cursor()
    cursor.execute('DELETE FROM history WHERE user_id = ?', (session['user_id'],))
    conn.commit()
    conn.close()
    
    log_activity(session['user_id'], 'clear_history', 'پاک کردن تاریخچه', request.remote_addr)
    return jsonify({'success': True})

# ==========================================
# اجرا
# ==========================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)