import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = 'super_secret_barber_key_123'

# --- Database Initialization ---
def init_db():
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, email TEXT, phone TEXT NOT NULL,
            date TEXT NOT NULL, time TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS blocked_times (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, time TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def generate_all_slots():
    slots = []
    start_time = datetime.strptime('08:00 AM', '%I:%M %p')
    end_time = datetime.strptime('09:00 PM', '%I:%M %p')
    current_time = start_time
    while current_time <= end_time:
        slots.append(current_time.strftime('%I:%M %p'))
        current_time += timedelta(minutes=30)
    return slots

# --- Public Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/available-slots')
def available_slots():
    selected_date = request.args.get('date')
    if not selected_date:
        return jsonify([])

    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    
    # 1. Get booked appointments
    cursor.execute('SELECT time FROM appointments WHERE date = ?', (selected_date,))
    booked_times = [row[0] for row in cursor.fetchall()]
    
    # 2. Figure out the specific day of the week
    dt = datetime.strptime(selected_date, '%Y-%m-%d')
    weekday_map = {
        0: 'EVERY_MONDAY', 1: 'EVERY_TUESDAY', 2: 'EVERY_WEDNESDAY',
        3: 'EVERY_THURSDAY', 4: 'EVERY_FRIDAY', 5: 'EVERY_SATURDAY', 6: 'EVERY_SUNDAY'
    }
    day_rule = weekday_map[dt.weekday()]
    
    # 3. Get admin blocks (Specific Date OR Daily OR the Specific Weekday)
    cursor.execute('''
        SELECT time FROM blocked_times 
        WHERE date = ? OR date = 'DAILY' OR date = ?
    ''', (selected_date, day_rule))
    admin_blocked = [row[0] for row in cursor.fetchall()]
        
    conn.close()

    unavailable_times = booked_times + admin_blocked
    all_slots = generate_all_slots()
    available = [slot for slot in all_slots if slot not in unavailable_times]

    return jsonify(available)

@app.route('/book', methods=['POST'])
def book():
    client_name = request.form.get('name', '').strip()
    client_email = request.form.get('email', '').strip()
    client_phone = request.form.get('phone', '').strip()
    booking_date = request.form.get('date', '').strip()
    booking_time = request.form.get('time', '').strip()

    if not client_name or not client_phone or not booking_date or not booking_time:
        return "Missing information.", 400

    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()

    # Double booking check
    cursor.execute('SELECT * FROM appointments WHERE date = ? AND time = ?', (booking_date, booking_time))
    if cursor.fetchone():
        conn.close(); return "Time slot already booked.", 409
        
    # Blocked time checks (Specific, Daily, or specific day of the week)
    dt = datetime.strptime(booking_date, '%Y-%m-%d')
    weekday_map = {
        0: 'EVERY_MONDAY', 1: 'EVERY_TUESDAY', 2: 'EVERY_WEDNESDAY',
        3: 'EVERY_THURSDAY', 4: 'EVERY_FRIDAY', 5: 'EVERY_SATURDAY', 6: 'EVERY_SUNDAY'
    }
    day_rule = weekday_map[dt.weekday()]
    
    cursor.execute('''
        SELECT * FROM blocked_times 
        WHERE (date = ? OR date = 'DAILY' OR date = ?) AND time = ?
    ''', (booking_date, day_rule, booking_time))
                   
    if cursor.fetchone():
        conn.close(); return "Time slot unavailable.", 409

    cursor.execute(
        'INSERT INTO appointments (name, email, phone, date, time) VALUES (?, ?, ?, ?, ?)',
        (client_name, client_email, client_phone, booking_date, booking_time)
    )
    conn.commit()
    conn.close()

    return render_template('success.html', name=client_name, email=client_email, date=booking_date, time=booking_time)

# --- Admin Routes ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['password'] == 'haircut2024':
            session['logged_in'] = True
            return redirect(url_for('admin'))
        else:
            return "<h1>Incorrect Password</h1><a href='/login'>Try again</a>", 403
    return render_template('login.html')

@app.route('/admin')
def admin():
    if not session.get('logged_in'): return redirect(url_for('login'))
    
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM appointments ORDER BY date ASC, time ASC')
    all_bookings = cursor.fetchall()
    
    # Sort blocks so DAILY is at the top, then EVERY_*, then specific dates
    cursor.execute('''
        SELECT * FROM blocked_times 
        ORDER BY 
            CASE 
                WHEN date = 'DAILY' THEN 1 
                WHEN date LIKE 'EVERY_%' THEN 2 
                ELSE 3 
            END, 
            date ASC, time ASC
    ''')
    blocked_times = cursor.fetchall()
    conn.close()

    upcoming_bookings = []
    past_bookings = []
    now = datetime.now()

    for booking in all_bookings:
        date_str, time_str = booking[4], booking[5]
        try:
            booking_dt = datetime.strptime(f"{date_str} {time_str}", '%Y-%m-%d %I:%M %p')
            if booking_dt >= now: upcoming_bookings.append(booking)
            else: past_bookings.append(booking)
        except ValueError:
            upcoming_bookings.append(booking)

    past_bookings.reverse()
    return render_template('admin.html', upcoming=upcoming_bookings, past=past_bookings, blocked=blocked_times)

@app.route('/delete/<int:id>', methods=['POST'])
def delete_appointment(id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    conn = sqlite3.connect('bookings.db')
    conn.cursor().execute('DELETE FROM appointments WHERE id = ?', (id,))
    conn.commit(); conn.close()
    return redirect(url_for('admin'))

@app.route('/admin/block', methods=['POST'])
def block_time():
    if not session.get('logged_in'): return redirect(url_for('login'))
    
    block_type = request.form.get('block_type')
    start_time_str = request.form.get('start_time')
    end_time_str = request.form.get('end_time')
    
    # Determine the actual string to save in the DB
    target_date = request.form.get('block_date')
    if block_type == 'daily': 
        target_date = 'DAILY'
    elif block_type.startswith('every_'): 
        target_date = block_type.upper()
    
    if target_date and start_time_str and end_time_str:
        try:
            start_dt = datetime.strptime(start_time_str, '%I:%M %p')
            end_dt = datetime.strptime(end_time_str, '%I:%M %p')
            
            if start_dt < end_dt:
                conn = sqlite3.connect('bookings.db')
                cursor = conn.cursor()
                current_dt = start_dt
                while current_dt < end_dt:
                    time_slot = current_dt.strftime('%I:%M %p')
                    cursor.execute('SELECT * FROM blocked_times WHERE date = ? AND time = ?', (target_date, time_slot))
                    if not cursor.fetchone():
                        cursor.execute('INSERT INTO blocked_times (date, time) VALUES (?, ?)', (target_date, time_slot))
                    current_dt += timedelta(minutes=30)
                conn.commit(); conn.close()
        except ValueError:
            pass

    return redirect(url_for('admin'))

@app.route('/admin/unblock/<int:id>', methods=['POST'])
def unblock_time(id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    conn = sqlite3.connect('bookings.db')
    conn.cursor().execute('DELETE FROM blocked_times WHERE id = ?', (id,))
    conn.commit(); conn.close()
    return redirect(url_for('admin'))

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=1000, debug=True)