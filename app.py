import asyncio
import time
import httpx
import json
from collections import defaultdict
from functools import wraps
from flask import Flask, request, jsonify
from flask_cors import CORS
from cachetools import TTLCache
from typing import Tuple
from proto import FreeFire_pb2, main_pb2, AccountPersonalShow_pb2
from google.protobuf import json_format, message
from google.protobuf.message import Message
from Crypto.Cipher import AES
import base64

# === Settings ===
MAIN_KEY = base64.b64decode('WWcmdGMlREV1aDYlWmNeOA==')
MAIN_IV = base64.b64decode('Nm95WkRyMjJFM3ljaGpNJQ==')
RELEASEVERSION = "OB49"
USERAGENT = "Dalvik/2.1.0 (Linux; U; Android 13; CPH2095 Build/RKQ1.211119.001)"
SUPPORTED_REGIONS = {"IND", "BR", "US", "SAC", "NA", "SG", "RU", "ID", "TW", "VN", "TH", "ME", "PK", "CIS", "BD", "EUROPE"}

# === Flask App Setup ===
app = Flask(__name__)
CORS(app)
cache = TTLCache(maxsize=100, ttl=300)
cached_tokens = defaultdict(dict)

# === Helper Functions ===
def pad(text: bytes) -> bytes:
    padding_length = AES.block_size - (len(text) % AES.block_size)
    return text + bytes([padding_length] * padding_length)

def aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    aes = AES.new(key, AES.MODE_CBC, iv)
    return aes.encrypt(pad(plaintext))

def decode_protobuf(encoded_data: bytes, message_type: message.Message) -> message.Message:
    instance = message_type()
    instance.ParseFromString(encoded_data)
    return instance

async def json_to_proto(json_data: str, proto_message: Message) -> bytes:
    json_format.ParseDict(json.loads(json_data), proto_message)
    return proto_message.SerializeToString()

def get_account_credentials(region: str) -> str:
    r = region.upper()
    if r == "IND":
        return "uid=3942037420&password=A0BF31A2E867E1619013C57462DFCF8D08102552EFB060FAE9A1213C3F331F25"
    elif r in {"BR", "US", "SAC", "NA"}:
        return "uid=uid&password=password"
    else:
        return "uid=uid&password=password"

# === Token Generation ===
async def get_access_token(account: str):
    url = "https://ffmconnect.live.gop.garenanow.com/oauth/guest/token/grant"
    payload = account + "&response_type=token&client_type=2&client_secret=2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3&client_id=100067"
    headers = {'User-Agent': USERAGENT, 'Connection': "Keep-Alive", 'Accept-Encoding': "gzip", 'Content-Type': "application/x-www-form-urlencoded"}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, data=payload, headers=headers)
        data = resp.json()
        return data.get("access_token", "0"), data.get("open_id", "0")

async def create_jwt(region: str):
    account = get_account_credentials(region)
    token_val, open_id = await get_access_token(account)
    body = json.dumps({"open_id": open_id, "open_id_type": "4", "login_token": token_val, "orign_platform_type": "4"})
    proto_bytes = await json_to_proto(body, FreeFire_pb2.LoginReq())
    payload = aes_cbc_encrypt(MAIN_KEY, MAIN_IV, proto_bytes)
    url = "https://loginbp.ggblueshark.com/MajorLogin"
    headers = {'User-Agent': USERAGENT, 'Connection': "Keep-Alive", 'Accept-Encoding': "gzip",
               'Content-Type': "application/octet-stream", 'Expect': "100-continue", 'X-Unity-Version': "2018.4.11f1",
               'X-GA': "v1 1", 'ReleaseVersion': RELEASEVERSION}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, data=payload, headers=headers)
        msg = json.loads(json_format.MessageToJson(decode_protobuf(resp.content, FreeFire_pb2.LoginRes)))
        cached_tokens[region] = {
            'token': f"Bearer {msg.get('token','0')}",
            'region': msg.get('lockRegion','0'),
            'server_url': msg.get('serverUrl','0'),
            'expires_at': time.time() + 25200
        }

async def initialize_tokens():
    tasks = [create_jwt(r) for r in SUPPORTED_REGIONS]
    await asyncio.gather(*tasks)

async def refresh_tokens_periodically():
    while True:
        await asyncio.sleep(25200)
        await initialize_tokens()

async def get_token_info(region: str) -> Tuple[str,str,str]:
    info = cached_tokens.get(region)
    if info and time.time() < info['expires_at']:
        return info['token'], info['region'], info['server_url']
    await create_jwt(region)
    info = cached_tokens[region]
    return info['token'], info['region'], info['server_url']

async def GetAccountInformation(uid, unk, region, endpoint):
    region = region.upper()
    if region not in SUPPORTED_REGIONS:
        raise ValueError(f"Unsupported region: {region}")
    payload = await json_to_proto(json.dumps({'a': uid, 'b': unk}), main_pb2.GetPlayerPersonalShow())
    data_enc = aes_cbc_encrypt(MAIN_KEY, MAIN_IV, payload)
    token, lock, server = await get_token_info(region)
    headers = {'User-Agent': USERAGENT, 'Connection': "Keep-Alive", 'Accept-Encoding': "gzip",
               'Content-Type': "application/octet-stream", 'Expect': "100-continue",
               'Authorization': token, 'X-Unity-Version': "2018.4.11f1", 'X-GA': "v1 1",
               'ReleaseVersion': RELEASEVERSION}
    async with httpx.AsyncClient() as client:
        resp = await client.post(server+endpoint, data=data_enc, headers=headers)
        proto_response = decode_protobuf(resp.content, AccountPersonalShow_pb2.AccountPersonalShowInfo)
        data = json_format.MessageToDict(proto_response)
        
        # Transform the data into your desired format
        transformed = {
            "AccountInfo": {
                "AccountID": data.get("basicInfo", {}).get("accountId"),
                "AccountType": data.get("basicInfo", {}).get("accountType"),
                "AccountNickname": data.get("basicInfo", {}).get("nickname"),
                "AccountRegion": data.get("basicInfo", {}).get("region"),
                "AccountLevel": data.get("basicInfo", {}).get("level"),
                "AccountEXP": data.get("basicInfo", {}).get("exp"),
                "AccountBannerID": data.get("basicInfo", {}).get("bannerId"),
                "AccountHeadPic": data.get("basicInfo", {}).get("headPic"),
                "AccountRank": data.get("basicInfo", {}).get("rank"),
                "AccountRankingPoints": data.get("basicInfo", {}).get("rankingPoints"),
                "AccountRole": data.get("basicInfo", {}).get("role"),
                "AccountHasElitePass": data.get("basicInfo", {}).get("hasElitePass"),
                "AccountBadgeCnt": data.get("basicInfo", {}).get("badgeCnt"),
                "AccountBadgeId": data.get("basicInfo", {}).get("badgeId"),
                "AccountSeasonId": data.get("basicInfo", {}).get("seasonId"),
                "AccountLiked": data.get("basicInfo", {}).get("liked"),
                "AccountLastLoginAt": data.get("basicInfo", {}).get("lastLoginAt"),
                "CsRank": data.get("basicInfo", {}).get("csRank"),
                "CsRankingPoints": data.get("basicInfo", {}).get("csRankingPoints"),
                "EquippedWeaponSkinShows": data.get("basicInfo", {}).get("weaponSkinShows", []),
                "MaxRank": data.get("basicInfo", {}).get("maxRank"),
                "CsMaxRank": data.get("basicInfo", {}).get("csMaxRank"),
                "AccountPrefers": data.get("basicInfo", {}).get("accountPrefers", {})
            },
            "AccountProfileInfo": {
                "avatarId": data.get("profileInfo", {}).get("avatarId"),
                "skinColor": data.get("profileInfo", {}).get("skinColor"),
                "clothes": data.get("profileInfo", {}).get("clothes", []),
                "equipedSkills": data.get("profileInfo", {}).get("equipedSkills", []),
                "isSelected": data.get("profileInfo", {}).get("isSelected"),
                "isSelectedAwaken": data.get("profileInfo", {}).get("isSelectedAwaken"),
                "clothesTailorEffects": data.get("profileInfo", {}).get("clothesTailorEffects", [])
            },
            "createAt": data.get("basicInfo", {}).get("createAt"),
            "title": data.get("basicInfo", {}).get("title"),
            "releaseVersion": data.get("basicInfo", {}).get("releaseVersion"),
            "showBrRank": data.get("basicInfo", {}).get("showBrRank"),
            "showCsRank": data.get("basicInfo", {}).get("showCsRank"),
            "socialHighLightsWithBasicInfo": data.get("basicInfo", {}).get("socialHighLightsWithBasicInfo", {}),
            "captainBasicInfo": {
                "accountId": data.get("basicInfo", {}).get("accountId"),
                "accountType": data.get("basicInfo", {}).get("accountType"),
                "nickname": data.get("basicInfo", {}).get("nickname"),
                "region": data.get("basicInfo", {}).get("region"),
                "level": data.get("basicInfo", {}).get("level"),
                "exp": data.get("basicInfo", {}).get("exp"),
                "bannerId": data.get("basicInfo", {}).get("bannerId"),
                "headPic": data.get("basicInfo", {}).get("headPic"),
                "rank": data.get("basicInfo", {}).get("rank"),
                "rankingPoints": data.get("basicInfo", {}).get("rankingPoints"),
                "role": data.get("basicInfo", {}).get("role"),
                "hasElitePass": data.get("basicInfo", {}).get("hasElitePass"),
                "badgeCnt": data.get("basicInfo", {}).get("badgeCnt"),
                "badgeId": data.get("basicInfo", {}).get("badgeId"),
                "seasonId": data.get("basicInfo", {}).get("seasonId"),
                "liked": data.get("basicInfo", {}).get("liked"),
                "lastLoginAt": data.get("basicInfo", {}).get("lastLoginAt"),
                "csRank": data.get("basicInfo", {}).get("csRank"),
                "csRankingPoints": data.get("basicInfo", {}).get("csRankingPoints"),
                "weaponSkinShows": data.get("basicInfo", {}).get("weaponSkinShows", []),
                "maxRank": data.get("basicInfo", {}).get("maxRank"),
                "csMaxRank": data.get("basicInfo", {}).get("csMaxRank"),
                "accountPrefers": data.get("basicInfo", {}).get("accountPrefers", {})
            },
            "clanBasicInfo": {
                "clanId": data.get("clanBasicInfo", {}).get("clanId"),
                "clanName": data.get("clanBasicInfo", {}).get("clanName"),
                "captainId": data.get("clanBasicInfo", {}).get("captainId"),
                "clanLevel": data.get("clanBasicInfo", {}).get("clanLevel"),
                "capacity": data.get("clanBasicInfo", {}).get("capacity"),
                "memberNum": data.get("clanBasicInfo", {}).get("memberNum")
            },
            "creditScoreInfo": {
                "creditScore": data.get("creditScoreInfo", {}).get("creditScore"),
                "rewardState": data.get("creditScoreInfo", {}).get("rewardState"),
                "periodicSummaryEndTime": data.get("creditScoreInfo", {}).get("periodicSummaryEndTime")
            },
            "petInfo": {
                "id": data.get("petInfo", {}).get("id"),
                "name": data.get("petInfo", {}).get("name"),
                "level": data.get("petInfo", {}).get("level"),
                "exp": data.get("petInfo", {}).get("exp"),
                "isSelected": data.get("petInfo", {}).get("isSelected"),
                "skinId": data.get("petInfo", {}).get("skinId"),
                "selectedSkillId": data.get("petInfo", {}).get("selectedSkillId")
            },
            "socialInfo": {
                "accountId": data.get("socialInfo", {}).get("accountId"),
                "language": data.get("socialInfo", {}).get("language"),
                "modePrefer": data.get("socialInfo", {}).get("modePrefer"),
                "signature": data.get("socialInfo", {}).get("signature")
            }
        }
        
        return transformed

# === Caching Decorator ===
def cached_endpoint(ttl=300):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            key = (request.path, tuple(request.args.items()))
            if key in cache:
                return cache[key]
            res = fn(*a, **k)
            cache[key] = res
            return res
        return wrapper
    return decorator

# === Flask Routes ===
@app.route('/api/player-info')
@cached_endpoint()
def get_account_info():
    region = request.args.get('region')
    uid = request.args.get('uid')

    if not uid:
        return jsonify({"error": "Please provide UID."}), 400
    if not region:
        return jsonify({"error": "Please provide REGION."}), 400

    try:
        return_data = asyncio.run(GetAccountInformation(uid, "7", region, "/GetPlayerPersonalShow"))
        return jsonify(return_data), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/refresh', methods=['GET','POST'])
def refresh_tokens_endpoint():
    try:
        asyncio.run(initialize_tokens())
        return jsonify({'message':'Tokens refreshed for all regions.'}),200
    except Exception as e:
        return jsonify({'error': f'Refresh failed: {e}'}),500

# === Startup ===
async def startup():
    await initialize_tokens()
    asyncio.create_task(refresh_tokens_periodically())

if __name__ == '__main__':
    asyncio.run(startup())
    app.run(host='0.0.0.0', port=5000, debug=True)