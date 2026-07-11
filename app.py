import os
import re
from datetime import datetime, timedelta

from azure.cosmos import CosmosClient, PartitionKey, exceptions
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
# NEW: Import CSRF and Rate Limiter libraries
from flask_wtf.csrf import CSRFProtect

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(24))
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'haircut2024')

# -------------------------------------------------------------------------
# 1. INITIALIZE CSRF PROTECTION
# -------------------------------------------------------------------------
# This automatically protects all POST, PUT, PATCH, and DELETE routes.
# If a form is submitted without a valid secret token, Flask rejects it with a 400 Bad Request.
csrf = CSRFProtect(app)

# -------------------------------------------------------------------------
# 2. INITIALIZE RATE LIMITING
# -------------------------------------------------------------------------
# Uses the user's IP address (get_remote_address) to track request counts.
# Default limits apply across all routes unless overridden.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"  # For multi-server production, switch to a Redis URI: "redis://localhost:6379"
)


# Optional: Custom error handler when a rate limit is exceeded
@app.errorhandler(429)
def ratelimit_handler(e):
    return f"<h1>Too Many Requests</h1><p>You have exceeded your rate limit: {e.description}. Please try again later.</p>", 429

# --- Cosmos DB Setup ---
COSMOS_ENDPOINT = os.environ.get('COSMOS_ENDPOINT')
COSMOS_KEY = os.environ.get('COSMOS_KEY')

if not COSMOS_ENDPOINT or not COSMOS_KEY:
    raise RuntimeError("COSMOS_ENDPOINT and COSMOS_KEY environment variables are not set.")

client = CosmosClient(COSMOS_ENDPOINT, COSMOS_KEY)

DATABASE_NAME = 'BarbershopDB'
CONTAINER_NAME = 'ScheduleData'

database = client.create_database_if_not_exists(id=DATABASE_NAME)
container = database.create_container_if_not_exists(
    id=CONTAINER_NAME,
    partition_key=PartitionKey(path="/type"),
    offer_throughput=400
)


# --- Helper Functions ---
def generate_all_slots():
    slots = []
    start_time = datetime.strptime('08:00 AM', '%I:%M %p')
    end_time = datetime.strptime('09:00 PM', '%I:%M %p')
    current_time = start_time
    while current_time <= end_time:
        slots.append(current_time.strftime('%I:%M %p'))
        current_time += timedelta(minutes=30)
    return slots


def is_valid_email(email):
    if not email:
        return True
    return re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', email) is not None


def is_valid_phone(phone):
    digits_only = re.sub(r'\D', '', phone)
    return 10 <= len(digits_only) <= 15


def get_weekday_rule(date_str):
    """Safely parses a YYYY-MM-DD string and returns the EVERY_DAY rule."""
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    weekday_map = {
        0: 'EVERY_MONDAY', 1: 'EVERY_TUESDAY', 2: 'EVERY_WEDNESDAY',
        3: 'EVERY_THURSDAY', 4: 'EVERY_FRIDAY', 5: 'EVERY_SATURDAY', 6: 'EVERY_SUNDAY'
    }
    return weekday_map[dt.weekday()]


# --- Public Routes ---
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/ping')
def ping():
    return "Server is up and running", 200


@app.route('/api/available-slots')
def available_slots():
    selected_date = request.args.get('date')
    if not selected_date:
        return jsonify([])

    # Fix 4: Exception guards for date parsing
    try:
        day_rule = get_weekday_rule(selected_date)
    except ValueError:
        return jsonify({"error": "Invalid date format. Expected YYYY-MM-DD."}), 400

    # Fix 3: Targeted partition queries instead of cross-partition
    query_booked = "SELECT c.time FROM c WHERE c.date = @date"
    booked_results = container.query_items(
        query=query_booked,
        parameters=[{"name": "@date", "value": selected_date}],
        partition_key="appointment"
    )
    booked_times = [item['time'] for item in booked_results]

    query_blocked = """
                    SELECT c.time \
                    FROM c
                    WHERE c.date = @date \
                       OR c.date = 'DAILY' \
                       OR c.date = @day_rule \
                    """
    blocked_results = container.query_items(
        query=query_blocked,
        parameters=[
            {"name": "@date", "value": selected_date},
            {"name": "@day_rule", "value": day_rule}
        ],
        partition_key="block"
    )
    admin_blocked = [item['time'] for item in blocked_results]

    unavailable_times = set(booked_times + admin_blocked)
    all_slots = generate_all_slots()
    available = [slot for slot in all_slots if slot not in unavailable_times]

    return jsonify(available)


@app.route('/book', methods=['POST'])
@limiter.limit("5 per minute")
def book():
    client_name = request.form.get('name', '').strip()
    client_email = request.form.get('email', '').strip()
    client_phone = request.form.get('phone', '').strip()
    booking_date = request.form.get('date', '').strip()
    booking_time = request.form.get('time', '').strip()

    if not client_name or not client_phone or not booking_date or not booking_time:
        return "Missing information.", 400
    if not is_valid_email(client_email):
        return "Invalid email address.", 400
    if not is_valid_phone(client_phone):
        return "Invalid mobile number.", 400

    # Fix 4: Validate date and time format before querying
    try:
        day_rule = get_weekday_rule(booking_date)
        datetime.strptime(booking_time, '%I:%M %p')
    except ValueError:
        return "Invalid date or time format.", 400

    # Check for blocked admin time (Fix 3: Targeted partition query)
    query_block_check = """
                        SELECT * \
                        FROM c
                        WHERE c.time = @time \
                          AND (c.date = @date OR c.date = 'DAILY' OR c.date = @day_rule) \
                        """
    existing_block = list(container.query_items(
        query=query_block_check,
        parameters=[
            {"name": "@date", "value": booking_date},
            {"name": "@day_rule", "value": day_rule},
            {"name": "@time", "value": booking_time}
        ],
        partition_key="block"
    ))

    if existing_block:
        return "Time slot unavailable.", 409

    # Fix 2: Prevent TOCTOU race conditions with deterministic IDs
    # If two users book at the exact same time, Cosmos DB rejects the second write natively.
    deterministic_id = f"{booking_date}_{booking_time}"

    try:
        container.create_item(body={
            'id': deterministic_id,
            'type': 'appointment',
            'name': client_name,
            'email': client_email,
            'phone': client_phone,
            'date': booking_date,
            'time': booking_time
        })
    except exceptions.CosmosResourceExistsError:
        return "Time slot already booked.", 409

    return render_template('success.html', name=client_name, email=client_email, date=booking_date, time=booking_time)


# --- Admin Routes ---
@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def login():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('admin'))
        else:
            return "<h1>Incorrect Password</h1><a href='/login'>Try again</a>", 403
    return render_template('login.html')


@app.route('/admin')
def admin():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    # Fix 5: Performance / Memory optimization.
    # Only fetch appointments from 30 days ago onward so database reads remain lightweight.
    cutoff_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')

    # Fix 3: Query within specific partitions
    all_bookings = list(container.query_items(
        query="SELECT * FROM c WHERE c.date >= @cutoff",
        parameters=[{"name": "@cutoff", "value": cutoff_date}],
        partition_key="appointment"
    ))

    all_bookings.sort(key=lambda x: (x['date'], x['time']))

    blocked_times = list(container.query_items(
        query="SELECT * FROM c",
        partition_key="block"
    ))

    def block_sort_key(b):
        date_val = b['date']
        priority = 1 if date_val == 'DAILY' else (2 if date_val.startswith('EVERY_') else 3)
        return (priority, date_val, b['time'])

    blocked_times.sort(key=block_sort_key)

    upcoming_bookings = []
    past_bookings = []

    # Fix 5: Server time reference point
    now = datetime.now()

    for booking in all_bookings:
        try:
            booking_dt = datetime.strptime(f"{booking['date']} {booking['time']}", '%Y-%m-%d %I:%M %p')
            if booking_dt >= now:
                upcoming_bookings.append(booking)
            else:
                past_bookings.append(booking)
        except ValueError:
            upcoming_bookings.append(booking)

    past_bookings.reverse()
    return render_template('admin.html', upcoming=upcoming_bookings, past=past_bookings, blocked=blocked_times)


@app.route('/delete/<string:id>', methods=['POST'])
@limiter.exempt
def delete_appointment(id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    try:
        container.delete_item(item=id, partition_key='appointment')
    except exceptions.CosmosResourceNotFoundError:
        pass
    return redirect(url_for('admin'))


@app.route('/admin/block', methods=['POST'])
@limiter.exempt
def block_time():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    block_type = request.form.get('block_type')
    start_time_str = request.form.get('start_time')
    end_time_str = request.form.get('end_time')

    target_date = request.form.get('block_date')
    if block_type == 'daily':
        target_date = 'DAILY'
    elif block_type and block_type.startswith('every_'):
        target_date = block_type.upper()

    if target_date and start_time_str and end_time_str:
        # Fix 4: Safely guard time block parsing
        try:
            start_dt = datetime.strptime(start_time_str, '%I:%M %p')
            end_dt = datetime.strptime(end_time_str, '%I:%M %p')

            if start_dt < end_dt:
                current_dt = start_dt
                while current_dt < end_dt:
                    time_slot = current_dt.strftime('%I:%M %p')

                    # Fix 2 & 3: Use deterministic IDs for blocks to avoid queries and race conditions
                    block_id = f"block_{target_date}_{time_slot}"
                    try:
                        container.create_item(body={
                            'id': block_id,
                            'type': 'block',
                            'date': target_date,
                            'time': time_slot
                        })
                    except exceptions.CosmosResourceExistsError:
                        pass  # Block already exists, skip silently

                    current_dt += timedelta(minutes=30)
        except ValueError:
            pass

    return redirect(url_for('admin'))


@app.route('/admin/unblock/<string:id>', methods=['POST'])
@limiter.exempt
def unblock_time(id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    try:
        container.delete_item(item=id, partition_key='block')
    except exceptions.CosmosResourceNotFoundError:
        pass
    return redirect(url_for('admin'))


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('index'))


if __name__ == '__main__':
    # Keep debug=False for safety if deploying to production
    app.run(host='0.0.0.0', port=1000, debug=False)
