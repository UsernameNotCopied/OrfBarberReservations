import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from azure.cosmos import CosmosClient, PartitionKey, exceptions
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(24))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("OrfBarber")

app.config.update(
    # Only send cookies over HTTPS (Set to False if testing locally on http://127.0.0.1)
    SESSION_COOKIE_SECURE=os.environ.get('FLASK_ENV') == 'production',
    # Prevent JavaScript from reading the session cookie (defends against XSS token theft)
    SESSION_COOKIE_HTTPONLY=True,
    # Restrict cookie transmission to same-site requests (defends against CSRF)
    SESSION_COOKIE_SAMESITE='Lax',
    # Automatically expire sessions after 2 hours of inactivity
    PERMANENT_SESSION_LIFETIME=timedelta(hours=2)
)

csp_policy = {
    'default-src': '\'self\'',
    'script-src': [
        '\'self\'',
        'https://cdn.jsdelivr.net',
        '\'unsafe-inline\''  # Required for your inline script blocks in index.html & admin.html
    ],
    'style-src': [
        '\'self\'',
        'https://cdn.jsdelivr.net',
        'https://fonts.googleapis.com',
        '\'unsafe-inline\''  # Required for inline CSS and Flatpickr UI theming
    ],
    'font-src': [
        '\'self\'',
        'https://fonts.gstatic.com'
    ],
    'img-src': [
        '\'self\'',
        'data:'  # Allows inline SVG/Base64 favicons
    ],
    'object-src': '\'none\'',  # Disallows Flash or legacy browser plugins entirely
}

Talisman(
    app,
    content_security_policy=csp_policy,
    # Force HTTPS only when running in production on Azure
    force_https=os.environ.get('FLASK_ENV') == 'production',
    # Prevents Clickjacking by forbidding any site from framing your app
    frame_options='DENY',
    # Tells browsers to remember HTTPS encryption for 1 full year (31,536,000 seconds)
    strict_transport_security=True,
    strict_transport_security_max_age=31536000,
    # Prevents browsers from guessing file types (mitigates drive-by downloads)
    x_content_type_options=True
)

# Replace plaintext ADMIN_PASSWORD with a hash.
# If ADMIN_PASSWORD_HASH isn't set in the environment, fallback to generating a hash for 'haircut2024' on startup.
ADMIN_PASSWORD_HASH = os.environ.get('ADMIN_PASSWORD_HASH')

ATHENS_TZ = ZoneInfo("Europe/Athens")

csrf = CSRFProtect(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"  # For multi-server production, switch to a Redis URI: "redis://localhost:6379"
)


# Optional: Custom error handler when a rate limit is exceeded
@app.errorhandler(429)
def ratelimit_handler(e):
    return f"<h1>Too Many Requests</h1><p>You have exceeded your rate limit. Please try again later.</p>", 429


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


def generate_all_slots():
    slots = []
    start_time = datetime.strptime('08:00 AM', '%I:%M %p')
    # Change end_time from 09:00 PM to 10:00 PM
    end_time = datetime.strptime('10:00 PM', '%I:%M %p')
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
@limiter.exempt
def index():
    return render_template('index.html')


@app.route('/ping')
@limiter.exempt
def ping():
    return "Server is up and running", 200


@app.route('/api/available-slots')
def available_slots():
    selected_date = request.args.get('date')
    if not selected_date:
        return jsonify([])

    try:
        day_rule = get_weekday_rule(selected_date)
    except ValueError:
        return jsonify({"error": "Invalid date format. Expected YYYY-MM-DD."}), 400

    # Enforce the 7-day maximum advance booking window
    now_athens = datetime.now(ATHENS_TZ)
    today_str = now_athens.strftime('%Y-%m-%d')
    max_allowed_date_str = (now_athens + timedelta(days=7)).strftime('%Y-%m-%d')

    if selected_date < today_str or selected_date > max_allowed_date_str:
        return jsonify([])  # Return empty slots if the requested date is out of bounds

    # Query booked appointments
    query_booked = "SELECT c.time FROM c WHERE c.date = @date"
    booked_results = container.query_items(
        query=query_booked,
        parameters=[{"name": "@date", "value": selected_date}],
        partition_key="appointment"
    )
    booked_times = [item['time'] for item in booked_results]

    # Query admin blocked slots
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

    # Calculate the 30-minute buffer cutoff in Athens time
    buffer_limit = now_athens + timedelta(minutes=30)

    available = []
    for slot in all_slots:
        # If the slot is already booked or blocked by admin, skip it
        if slot in unavailable_times:
            continue

        # If the selected date is today, check if the slot is at least 30 minutes away
        if selected_date == today_str:
            slot_dt = datetime.strptime(f"{selected_date} {slot}", '%Y-%m-%d %I:%M %p').replace(tzinfo=ATHENS_TZ)
            if slot_dt < buffer_limit:
                continue  # Skip slots in the past or within the 30-minute buffer

        available.append(slot)

    return jsonify(available)


@app.route('/book', methods=['POST'])
@limiter.limit("3 per hour")
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

    try:
        day_rule = get_weekday_rule(booking_date)
        # Parse into a datetime object to validate format
        parsed_time = datetime.strptime(booking_time, '%I:%M %p')
    except ValueError:
        return "Invalid date or time format.", 400

    # Enforce the 30-minute advance buffer and 7-day maximum booking window in Athens time
    now_athens = datetime.now(ATHENS_TZ)
    buffer_limit = now_athens + timedelta(minutes=30)
    max_allowed_date = now_athens + timedelta(days=7)

    booking_dt = datetime.strptime(f"{booking_date} {booking_time}", '%Y-%m-%d %I:%M %p').replace(tzinfo=ATHENS_TZ)

    if booking_dt < buffer_limit:
        return "Appointments must be booked at least 30 minutes in advance.", 400

    # Reject bookings more than 7 days in advance
    if booking_dt > max_allowed_date:
        return "Appointments can only be booked up to 7 days in advance.", 400

    # Check for blocked admin time (Targeted partition query)
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

    # Prevent TOCTOU race conditions with deterministic IDs
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
        submitted_password = request.form.get('password', '')

        # Securely verify the submitted password against the stored hash
        if check_password_hash(ADMIN_PASSWORD_HASH, submitted_password):
            session.permanent = True  # Enforce the 2-hour PERMANENT_SESSION_LIFETIME
            session['logged_in'] = True
            return redirect(url_for('admin'))
        else:
            return "<h1>Incorrect Password</h1><a href='/login'>Try again</a>", 403

    return render_template('login.html')


@app.route('/admin')
def admin():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    # Get current time localized to Athens
    now = datetime.now(ATHENS_TZ)
    cutoff_date = (now - timedelta(days=30)).strftime('%Y-%m-%d')

    # Query within specific partitions
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

    # --- UPDATED CHRONOLOGICAL SORTING FUNCTION ---
    def block_sort_key(b):
        date_val = b['date']

        # 1. Group by rule priority: Daily -> Weekly -> Specific Dates
        priority = 1 if date_val == 'DAILY' else (2 if date_val.startswith('EVERY_') else 3)

        # 2. Convert 'hh:mm AM/PM' string into a real Python datetime object
        # This forces Python to sort by actual time of day (e.g., 08:00 before 13:00)
        try:
            time_obj = datetime.strptime(b['time'], '%I:%M %p')
        except ValueError:
            time_obj = datetime.min  # Fallback safety if data is corrupted

        return (priority, date_val, time_obj)

    # ----------------------------------------------

    blocked_times.sort(key=block_sort_key)

    upcoming_bookings = []
    past_bookings = []

    for booking in all_bookings:
        try:
            booking_dt = datetime.strptime(f"{booking['date']} {booking['time']}", '%Y-%m-%d %I:%M %p')
            booking_dt = booking_dt.replace(tzinfo=ATHENS_TZ)

            if booking_dt >= now:
                upcoming_bookings.append(booking)
            else:
                past_bookings.append(booking)
        except ValueError as e:
            # Log the exact record causing the parsing failure so you can fix it in the database
            logger.error(f"Corrupted date/time format for booking ID {booking.get('id', 'UNKNOWN')}: {str(e)}")
            upcoming_bookings.append(booking)

    past_bookings.reverse()
    return render_template('admin.html', upcoming=upcoming_bookings, past=past_bookings, blocked=blocked_times)


@app.route('/delete/<string:id>', methods=['POST'])
@limiter.exempt
def delete_appointment(id):
    if not session.get('logged_in'):
        logger.warning(f"Unauthorized deletion attempt for appointment ID: {id}")
        return redirect(url_for('login'))

    try:
        container.delete_item(item=id, partition_key='appointment')
        logger.info(f"Admin successfully deleted appointment ID: {id}")
    except exceptions.CosmosResourceNotFoundError:
        logger.warning(f"Attempted to delete non-existent appointment ID: {id}")
    except Exception as e:
        logger.error(f"Unexpected database error while deleting appointment ID {id}: {str(e)}", exc_info=True)

    return redirect(url_for('admin'))


@app.route('/admin/block', methods=['POST'])
@limiter.exempt
def block_time():
    if not session.get('logged_in'):
        logger.warning("Unauthorized attempt to access /admin/block")
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
        try:
            start_dt = datetime.strptime(start_time_str, '%I:%M %p')
            end_dt = datetime.strptime(end_time_str, '%I:%M %p')

            if start_dt < end_dt:
                current_dt = start_dt
                while current_dt < end_dt:
                    time_slot = current_dt.strftime('%I:%M %p')
                    block_id = f"block_{target_date}_{time_slot}"

                    try:
                        container.create_item(body={
                            'id': block_id,
                            'type': 'block',
                            'date': target_date,
                            'time': time_slot
                        })
                        logger.info(f"Created block rule [{block_id}] for {target_date} at {time_slot}")
                    except exceptions.CosmosResourceExistsError:
                        logger.info(f"Block rule [{block_id}] already exists. Skipped duplicate creation.")
                    except Exception as e:
                        logger.error(f"Failed to write block rule [{block_id}] to Cosmos DB: {str(e)}", exc_info=True)

                    current_dt += timedelta(minutes=30)
            else:
                logger.warning(
                    f"Admin submitted invalid time block range: start ({start_time_str}) >= end ({end_time_str})")
        except ValueError as e:
            logger.error(f"Time format parsing error in block_time: {str(e)}", exc_info=True)
    else:
        logger.warning("Admin submitted incomplete block rule form data.")

    return redirect(url_for('admin'))


@app.route('/admin/unblock/<string:id>', methods=['POST'])
@limiter.exempt
def unblock_time(id):
    if not session.get('logged_in'):
        logger.warning(f"Unauthorized unblock attempt for block ID: {id}")
        return redirect(url_for('login'))

    try:
        container.delete_item(item=id, partition_key='block')
        logger.info(f"Admin successfully removed block rule ID: {id}")
    except exceptions.CosmosResourceNotFoundError:
        logger.warning(f"Attempted to unblock non-existent block ID: {id}")
    except Exception as e:
        logger.error(f"Unexpected error while removing block ID {id}: {str(e)}", exc_info=True)

    return redirect(url_for('admin'))


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('index'))


if __name__ == '__main__':
    # Keep debug=False for safety if deploying to production
    app.run(host='0.0.0.0', port=1000, debug=False)