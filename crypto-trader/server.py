"""
업비트 자동매매 백엔드 서버
- 포트 5000에서 실행
- 업비트 private API를 안전하게 프록시
"""
import os, uuid, hashlib, urllib.parse, logging
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS
import jwt
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)  # 로컬 프론트(8080)에서의 요청 허용

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

ACCESS_KEY = os.getenv('UPBIT_ACCESS_KEY', '')
SECRET_KEY = os.getenv('UPBIT_SECRET_KEY', '')
UPBIT_URL  = 'https://api.upbit.com/v1'

# 주문 기록 (실거래 로그)
order_log = []


# ── JWT 생성 ──────────────────────────────────
def make_jwt(query: dict = None) -> str:
    payload = {
        'access_key': ACCESS_KEY,
        'nonce': str(uuid.uuid4()),
    }
    if query:
        encoded = urllib.parse.urlencode(query).encode()
        m = hashlib.sha512()
        m.update(encoded)
        payload['query_hash']     = m.hexdigest()
        payload['query_hash_alg'] = 'SHA512'
    return jwt.encode(payload, SECRET_KEY, algorithm='HS256')


def upbit_get(path: str, params: dict = None):
    token   = make_jwt(params)
    headers = {'Authorization': f'Bearer {token}'}
    url     = f'{UPBIT_URL}{path}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    return requests.get(url, headers=headers, timeout=10)


def upbit_post(path: str, body: dict):
    token   = make_jwt(body)
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }
    return requests.post(f'{UPBIT_URL}{path}', json=body, headers=headers, timeout=10)


def upbit_delete(path: str, params: dict):
    token   = make_jwt(params)
    headers = {'Authorization': f'Bearer {token}'}
    url     = f'{UPBIT_URL}{path}?' + urllib.parse.urlencode(params)
    return requests.delete(url, headers=headers, timeout=10)


# ── 상태 확인 ─────────────────────────────────
@app.route('/api/status')
def status():
    configured = bool(ACCESS_KEY and SECRET_KEY
                      and ACCESS_KEY != '여기에_액세스키_입력')
    return jsonify({
        'ok': True,
        'configured': configured,
        'time': datetime.now().isoformat(),
    })


# ── 잔고 조회 ─────────────────────────────────
@app.route('/api/accounts')
def get_accounts():
    try:
        res = upbit_get('/accounts')
        data = res.json()
        if isinstance(data, list):
            log.info(f'잔고 조회 성공: {len(data)}개 자산')
            return jsonify({'ok': True, 'data': data})
        return jsonify({'ok': False, 'error': data}), 400
    except Exception as e:
        log.error(f'잔고 조회 실패: {e}')
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── 시장가 매수 ───────────────────────────────
@app.route('/api/order/buy', methods=['POST'])
def buy():
    """
    body: { market: 'KRW-BTC', price: 100000 }  (KRW 금액)
    """
    body = request.json or {}
    market = body.get('market')
    price  = body.get('price')  # KRW 금액

    if not market or not price:
        return jsonify({'ok': False, 'error': '파라미터 오류'}), 400

    price = int(price)
    if price < 5000:
        return jsonify({'ok': False, 'error': '최소 주문 금액은 ₩5,000입니다'}), 400

    order_body = {
        'market':   market,
        'side':     'bid',
        'price':    str(price),
        'ord_type': 'price',   # 시장가 매수 (KRW 지정)
    }

    try:
        res  = upbit_post('/orders', order_body)
        data = res.json()

        if 'uuid' in data:
            log.info(f'[BUY] {market} ₩{price:,} → uuid:{data["uuid"]}')
            order_log.append({
                'time': datetime.now().isoformat(),
                'side': 'BUY', 'market': market,
                'price': price, 'uuid': data['uuid'],
            })
            return jsonify({'ok': True, 'data': data})

        log.warning(f'[BUY] 오류 응답: {data}')
        return jsonify({'ok': False, 'error': data}), 400

    except Exception as e:
        log.error(f'[BUY] 예외: {e}')
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── 시장가 매도 ───────────────────────────────
@app.route('/api/order/sell', methods=['POST'])
def sell():
    """
    body: { market: 'KRW-BTC', volume: 0.001 }  (코인 수량)
    """
    body   = request.json or {}
    market = body.get('market')
    volume = body.get('volume')  # 코인 수량

    if not market or not volume:
        return jsonify({'ok': False, 'error': '파라미터 오류'}), 400

    # 소수점 8자리 (업비트 정밀도)
    volume_str = f'{float(volume):.8f}'

    order_body = {
        'market':   market,
        'side':     'ask',
        'volume':   volume_str,
        'ord_type': 'market',  # 시장가 매도
    }

    try:
        res  = upbit_post('/orders', order_body)
        data = res.json()

        if 'uuid' in data:
            log.info(f'[SELL] {market} {volume_str} → uuid:{data["uuid"]}')
            order_log.append({
                'time': datetime.now().isoformat(),
                'side': 'SELL', 'market': market,
                'volume': float(volume), 'uuid': data['uuid'],
            })
            return jsonify({'ok': True, 'data': data})

        log.warning(f'[SELL] 오류 응답: {data}')
        return jsonify({'ok': False, 'error': data}), 400

    except Exception as e:
        log.error(f'[SELL] 예외: {e}')
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── 체결된 주문 내역 ──────────────────────────
@app.route('/api/orders/done')
def orders_done():
    market = request.args.get('market', 'KRW-BTC')
    try:
        params = {'market': market, 'state': 'done', 'limit': 20, 'order_by': 'desc'}
        res    = upbit_get('/orders', params)
        data   = res.json()
        return jsonify({'ok': True, 'data': data if isinstance(data, list) else []})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── 특정 주문 상세 조회 ───────────────────────
@app.route('/api/order/<uuid_str>')
def get_order(uuid_str):
    try:
        params = {'uuid': uuid_str}
        res    = upbit_get('/order', params)
        return jsonify({'ok': True, 'data': res.json()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── 현재 보유 코인 수량 조회 ──────────────────
@app.route('/api/balance/<coin>')
def get_coin_balance(coin):
    """coin: BTC, ETH, XRP ... (KRW 제외)"""
    try:
        res  = upbit_get('/accounts')
        data = res.json()
        if not isinstance(data, list):
            return jsonify({'ok': False, 'error': data}), 400

        krw_bal  = 0.0
        coin_bal = 0.0
        for item in data:
            if item['currency'] == 'KRW':
                krw_bal = float(item['balance'])
            if item['currency'] == coin:
                coin_bal = float(item['balance'])

        return jsonify({'ok': True, 'krw': krw_bal, 'coin': coin_bal, 'currency': coin})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


if __name__ == '__main__':
    key_ok = ACCESS_KEY and ACCESS_KEY != 'ACCESS_KEY' and '여기에' not in ACCESS_KEY
    print('=' * 50)
    print('  Upbit AutoBot Server')
    print('  Port: 5000')
    print(f'  API Key: {"OK" if key_ok else "NOT SET - check .env"}')
    print('=' * 50)
    app.run(port=5000, debug=False)
