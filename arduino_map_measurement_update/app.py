from math import asin, cos, radians, sin, sqrt
from pathlib import Path
import os
import secrets
import sqlite3

from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", BASE_DIR / "measurement.db"))
CENTER_LAT = 37.53643813
CENTER_LON = 127.2026826
ALLOWED_RADIUS_M = 250
DEVICE_API_KEY = os.environ.get("DEVICE_API_KEY", "")
DELETE_API_KEY = os.environ.get("DELETE_API_KEY") or (
    "local-delete-key" if not os.environ.get("RENDER") else ""
)
app = Flask(__name__)


def device_authorized():
    """기기 전용 API는 Render 환경변수에 등록된 비밀키를 요구합니다."""
    if not DEVICE_API_KEY:
        return False, (jsonify({"error": "서버에 DEVICE_API_KEY가 설정되지 않았습니다."}), 503)
    supplied_key = request.headers.get("X-Device-Key", "")
    if not secrets.compare_digest(supplied_key, DEVICE_API_KEY):
        return False, (jsonify({"error": "기기 인증에 실패했습니다."}), 401)
    return True, None


def delete_authorized():
    """기록 삭제에는 별도의 관리자 키를 요구합니다."""
    if not DELETE_API_KEY:
        return False, (jsonify({"error": "서버에 DELETE_API_KEY가 설정되지 않았습니다."}), 503)
    supplied_key = request.headers.get("X-Delete-Key", "")
    if not secrets.compare_digest(supplied_key, DELETE_API_KEY):
        return False, (jsonify({"error": "삭제 인증에 실패했습니다."}), 401)
    return True, None


def connect_db():
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    """기존 측정 DB는 유지하면서 지도/요청에 필요한 열을 추가합니다."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect_db() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS measurements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                temperature REAL,
                humidity REAL,
                brightness REAL
            )
        """)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(measurements)")}
        if "latitude" not in columns:
            connection.execute("ALTER TABLE measurements ADD COLUMN latitude REAL")
        if "longitude" not in columns:
            connection.execute("ALTER TABLE measurements ADD COLUMN longitude REAL")
        if "brightness" not in columns:
            connection.execute("ALTER TABLE measurements ADD COLUMN brightness REAL")
        if "request_id" not in columns:
            connection.execute("ALTER TABLE measurements ADD COLUMN request_id INTEGER")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS measurement_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)


def distance_m(lat1, lon1, lat2, lon2):
    """두 좌표 사이의 거리를 미터로 계산합니다."""
    earth_radius = 6371000
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    value = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return 2 * earth_radius * asin(sqrt(value))


def request_row(row):
    item = dict(row)
    item["distance_m"] = round(distance_m(CENTER_LAT, CENTER_LON, item["latitude"], item["longitude"]))
    return item


# WSGI 서버로 실행할 때도 테이블/필요한 열이 준비되도록 앱을 불러올 때 초기화합니다.
initialize_database()


@app.route("/")
def home():
    return render_template("index.html", center_lat=CENTER_LAT, center_lon=CENTER_LON)


@app.route("/api/requests", methods=["GET"])
def list_requests():
    with connect_db() as connection:
        rows = connection.execute("""
            SELECT r.id, r.latitude, r.longitude, r.status, r.created_at, r.completed_at,
                   m.temperature, m.humidity, m.brightness, m.time AS measured_at
            FROM measurement_requests r
            LEFT JOIN measurements m ON m.request_id = r.id
            ORDER BY r.id DESC LIMIT 100
        """).fetchall()
    return jsonify({"requests": [request_row(row) for row in rows]})


@app.route("/api/requests", methods=["POST"])
def create_request():
    data = request.get_json(silent=True) or {}
    try:
        latitude = float(data["latitude"])
        longitude = float(data["longitude"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "지도에서 위치를 먼저 선택해 주세요."}), 400

    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return jsonify({"error": "위치 좌표가 올바르지 않습니다."}), 400
    distance = distance_m(CENTER_LAT, CENTER_LON, latitude, longitude)
    if distance > ALLOWED_RADIUS_M:
        return jsonify({"error": "측정 위치는 남한고 중심 250m 범위 안에서 선택해 주세요."}), 400

    with connect_db() as connection:
        cursor = connection.execute(
            "INSERT INTO measurement_requests (latitude, longitude) VALUES (?, ?)",
            (latitude, longitude),
        )
        request_id = cursor.lastrowid
    return jsonify({"id": request_id, "status": "pending", "message": "측정 요청을 등록했습니다."}), 201


@app.route("/api/manual-measurements", methods=["POST"])
def add_manual_brightness():
    """선택한 지도 위치에 사용자가 직접 입력한 밝기를 저장합니다."""
    data = request.get_json(silent=True) or {}
    try:
        latitude = float(data["latitude"])
        longitude = float(data["longitude"])
        brightness = float(data["brightness"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "위치와 숫자 형태의 밝기 값이 필요합니다."}), 400

    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return jsonify({"error": "위치 좌표가 올바르지 않습니다."}), 400
    if brightness < 0:
        return jsonify({"error": "밝기는 0 이상의 숫자를 입력해 주세요."}), 400
    if distance_m(CENTER_LAT, CENTER_LON, latitude, longitude) > ALLOWED_RADIUS_M:
        return jsonify({"error": "측정 위치는 남한고 중심 250m 범위 안에서 선택해 주세요."}), 400

    with connect_db() as connection:
        cursor = connection.execute(
            "INSERT INTO measurement_requests (latitude, longitude, status, completed_at) "
            "VALUES (?, ?, 'completed', CURRENT_TIMESTAMP)",
            (latitude, longitude),
        )
        request_id = cursor.lastrowid
        connection.execute(
            "INSERT INTO measurements (brightness, latitude, longitude, request_id) VALUES (?, ?, ?, ?)",
            (brightness, latitude, longitude, request_id),
        )
    return jsonify({"id": request_id, "status": "completed", "message": "밝기 값을 저장했습니다."}), 201


@app.route("/api/requests/<int:request_id>", methods=["DELETE"])
def delete_measurement_request(request_id):
    """확인된 요청 한 건과 그 요청에 연결된 측정값만 삭제합니다."""
    authorized, error_response = delete_authorized()
    if not authorized:
        return error_response
    with connect_db() as connection:
        target = connection.execute(
            "SELECT id FROM measurement_requests WHERE id=?", (request_id,)
        ).fetchone()
        if target is None:
            return jsonify({"error": "삭제할 기록을 찾을 수 없습니다."}), 404
        connection.execute("DELETE FROM measurements WHERE request_id=?", (request_id,))
        connection.execute("DELETE FROM measurement_requests WHERE id=?", (request_id,))
    return jsonify({"message": "선택한 기록을 삭제했습니다."})


@app.route("/api/device/pending", methods=["GET"])
def device_pending():
    """ESP32가 측정기 버튼을 눌렀을 때 대기 요청을 가져옵니다."""
    authorized, error_response = device_authorized()
    if not authorized:
        return error_response
    with connect_db() as connection:
        row = connection.execute(
            "SELECT id, latitude, longitude FROM measurement_requests WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
    return jsonify({"request": dict(row) if row else None})


@app.route("/api/device/complete", methods=["POST"])
def device_complete():
    """ESP32가 센서 측정값을 보내면 요청 위치와 함께 저장합니다."""
    authorized, error_response = device_authorized()
    if not authorized:
        return error_response
    data = request.get_json(silent=True) or {}
    try:
        request_id = int(data["request_id"])
        temperature = float(data["temperature"])
        humidity = float(data["humidity"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "request_id, temperature, humidity 값이 필요합니다."}), 400
    if not (-80 <= temperature <= 100 and 0 <= humidity <= 100):
        return jsonify({"error": "온도 또는 습도 값의 범위를 확인해 주세요."}), 400

    with connect_db() as connection:
        target = connection.execute(
            "SELECT id FROM measurement_requests WHERE id=? AND status='pending'", (request_id,)
        ).fetchone()
        if target is None:
            return jsonify({"error": "대기 중인 측정 요청을 찾을 수 없습니다."}), 404
        connection.execute(
            "INSERT INTO measurements (temperature, humidity, latitude, longitude, request_id) "
            "SELECT ?, ?, latitude, longitude, id FROM measurement_requests WHERE id=?",
            (temperature, humidity, request_id),
        )
        connection.execute(
            "UPDATE measurement_requests SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE id=?",
            (request_id,),
        )
    return jsonify({"message": "측정값을 지정한 위치에 저장했습니다."}), 201


if __name__ == "__main__":
    print(f"DB 파일: {DATABASE_PATH}")
    print("브라우저에서 http://127.0.0.1:5000 을 여세요.")
    print("이 실행은 내 컴퓨터에서 확인하는 개발용입니다. 인터넷 배포 전에는 기기 API 인증을 추가해야 합니다.")
    app.run(debug=False)
