import os
import re
import uuid
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from datetime import datetime, timedelta
from azure.cosmos import CosmosClient, PartitionKey, exceptions

app = Flask(__name__)
app.secret_key = 'super_secret_barber_key_123'

# --- Cosmos DB Setup ---
COSMOS_ENDPOINT = os.environ.get('COSMOS_ENDPOINT')
COSMOS_KEY = os.environ.get('COSMOS_KEY')

if not COSMOS_ENDPOINT or not COSMOS_KEY:
    raise RuntimeError("COSMOS_ENDPOINT and COSMOS_KEY environment variables are not set.")

client = CosmosClient(COSMOS_ENDPOINT, COSMOS_KEY)

# Initialize Database and Container
DATABASE_NAME = 'BarbershopDB'
CONTAINER_NAME = 'ScheduleData'

database = client.create_database_if_not_exists(id=DATABASE_NAME)
# We partition by 'type' so 'appointment' and 'block' documents are grouped logically
container = database.create_container_if_not_exists(
    id=CONTAINER_NAME,
    partition_key=PartitionKey(path="/type"),
    offer_throughput=400  # Fits well within the free tier
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
    if not email: return True
    return re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', email) is not None


def is_valid_phone(phone):
    digits_only = re.sub(r'\D', '', phone)
    return 10 <= len(digits_only) <= 15


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

    # 1. Get booked appointments
    query_booked = "SELECT c.time FROM c WHERE c.type = 'appointment' AND c.date = @date"
    booked_results = container.query_items(
        query=query_booked,
        parameters=[{"name": "@date", "value": selected_date}],
        enable_cross_partition_query=True
    )
    booked_times = [item['time'] for item in booked_results]

    # 2. Figure out the specific day of the week
    dt = datetime.strptime(selected_date, '%Y-%m-%d')
    weekday_map = {
        0: 'EVERY_MONDAY', 1: 'EVERY_TUESDAY', 2: 'EVERY_WEDNESDAY',
        3: 'EVERY_THURSDAY', 4: 'EVERY_FRIDAY', 5: 'EVERY_SATURDAY', 6: 'EVERY_SUNDAY'
    }
    day_rule = weekday_map[dt.weekday()]

    # 3. Get admin blocks
    query_blocked = """
                    SELECT c.time \
                    FROM c
                    WHERE c.type = 'block'
                      AND (c.date = @date OR c.date = 'DAILY' OR c.date = @day_rule) \
                    """
    blocked_results = container.query_items(
        query=query_blocked,
        parameters=[
            {"name": "@date", "value": selected_date},
            {"name": "@day_rule", "value": day_rule}
        ],
        enable_cross_partition_query=True
    )
    admin_blocked = [item['time'] for item in blocked_results]

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
    if not is_valid_email(client_email): return "Invalid email address.", 400
    if not is_valid_phone(client_phone): return "Invalid mobile number.", 400

    # Double booking check
    query_check = "SELECT * FROM c WHERE c.type = 'appointment' AND c.date = @date AND c.time = @time"
    existing_booking = list(container.query_items(
        query=query_check,
        parameters=[{"name": "@date", "value": booking_date}, {"name": "@time", "value": booking_time}],
        enable_cross_partition_query=True
    ))
    if existing_booking:
        return "Time slot already booked.", 409

    # Blocked time checks
    dt = datetime.strptime(booking_date, '%Y-%m-%d')
    weekday_map = {
        0: 'EVERY_MONDAY', 1: 'EVERY_TUESDAY', 2: 'EVERY_WEDNESDAY',
        3: 'EVERY_THURSDAY', 4: 'EVERY_FRIDAY', 5: 'EVERY_SATURDAY', 6: 'EVERY_SUNDAY'
    }
    day_rule = weekday_map[dt.weekday()]

    query_block_check = """
                        SELECT * \
                        FROM c
                        WHERE c.type = 'block' \
                          AND c.time = @time
                          AND (c.date = @date OR c.date = 'DAILY' OR c.date = @day_rule) \
                        """
    existing_block = list(container.query_items(
        query=query_block_check,
        parameters=[
            {"name": "@date", "value": booking_date},
            {"name": "@day_rule", "value": day_rule},
            {"name": "@time", "value": booking_time}
        ],
        enable_cross_partition_query=True
    ))

    if existing_block:
        return "Time slot unavailable.", 409

    # Insert new appointment as a JSON document
    container.create_item(body={
        'id': str(uuid.uuid4()),  # Cosmos requires string IDs
        'type': 'appointment',
        'name': client_name,
        'email': client_email,
        'phone': client_phone,
        'date': booking_date,
        'time': booking_time
    })

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

    # Fetch all appointments
    all_bookings = list(container.query_items(
        query="SELECT * FROM c WHERE c.type = 'appointment'",
        enable_cross_partition_query=True
    ))

    # Sort appointments by date and time
    all_bookings.sort(key=lambda x: (x['date'], x['time']))

    # Fetch all blocks
    blocked_times = list(container.query_items(
        query="SELECT * FROM c WHERE c.type = 'block'",
        enable_cross_partition_query=True
    ))

    # Sort blocks natively in Python to mimic your SQL CASE WHEN logic
    def block_sort_key(b):
        date_val = b['date']
        if date_val == 'DAILY':
            priority = 1
        elif date_val.startswith('EVERY_'):
            priority = 2
        else:
            priority = 3
        return (priority, date_val, b['time'])

    blocked_times.sort(key=block_sort_key)

    upcoming_bookings = []
    past_bookings = []
    now = datetime.now()

    for booking in all_bookings:
        date_str, time_str = booking['date'], booking['time']
        try:
            booking_dt = datetime.strptime(f"{date_str} {time_str}", '%Y-%m-%d %I:%M %p')
            if booking_dt >= now:
                upcoming_bookings.append(booking)
            else:
                past_bookings.append(booking)
        except ValueError:
            upcoming_bookings.append(booking)

    past_bookings.reverse()
    return render_template('admin.html', upcoming=upcoming_bookings, past=past_bookings, blocked=blocked_times)


@app.route('/delete/<string:id>', methods=['POST'])
def delete_appointment(id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    try:
        # Cosmos requires both the item ID and the partition key to delete
        container.delete_item(item=id, partition_key='appointment')
    except exceptions.CosmosResourceNotFoundError:
        pass
    return redirect(url_for('admin'))


@app.route('/admin/block', methods=['POST'])
def block_time():
    if not session.get('logged_in'): return redirect(url_for('login'))

    block_type = request.form.get('block_type')
    start_time_str = request.form.get('start_time')
    end_time_str = request.form.get('end_time')

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
                current_dt = start_dt
                while current_dt < end_dt:
                    time_slot = current_dt.strftime('%I:%M %p')

                    # Check if block exists
                    query = "SELECT * FROM c WHERE c.type='block' AND c.date=@date AND c.time=@time"
                    existing = list(container.query_items(
                        query=query,
                        parameters=[{"name": "@date", "value": target_date}, {"name": "@time", "value": time_slot}],
                        enable_cross_partition_query=True
                    ))

                    # If not, create it
                    if not existing:
                        container.create_item(body={
                            'id': str(uuid.uuid4()),
                            'type': 'block',
                            'date': target_date,
                            'time': time_slot
                        })
                    current_dt += timedelta(minutes=30)
        except ValueError:
            pass

    return redirect(url_for('admin'))


@app.route('/admin/unblock/<string:id>', methods=['POST'])
def unblock_time(id):
    if not session.get('logged_in'): return redirect(url_for('login'))
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
    app.run(host='0.0.0.0', port=1000, debug=True)