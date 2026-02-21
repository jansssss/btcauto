"""
server.py - Flask dev server with SSE log streaming
"""
from flask import Flask, Response, send_from_directory
import time
import os

app = Flask(__name__)

LOG_FILE = '/tmp/bot.log'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/api/logs/stream')
def stream_logs():
    def generate():
        # Wait for log file to exist
        while not os.path.exists(LOG_FILE):
            yield "data: [로그 파일 대기 중...]\n\n"
            time.sleep(1)

        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            # Send existing lines first
            for line in f:
                line = line.rstrip()
                if line:
                    yield f"data: {line}\n\n"
            # Tail for new lines
            while True:
                line = f.readline()
                if line:
                    line = line.rstrip()
                    if line:
                        yield f"data: {line}\n\n"
                else:
                    time.sleep(0.2)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Access-Control-Allow-Origin': '*',
        }
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)
