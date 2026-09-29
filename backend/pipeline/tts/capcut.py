"""CapCut TTS client (TTS-only). Adapted from K07VN/capcut-tts-api — httpx, no STT/upload."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

BASE = "https://editor-api-sg.capcutapi.com"

DEFAULT_DEVICE = {
    "aid": "359289",
    "app_name": "CapCut",
    "appvr": "8.7.0",
    "version_name": "8.7.0",
    "version_code": "8.7.0",
    "channel": "capcutpc_google",
    "device_platform": "mac",
    "device_type": "MacBookPro17,1",
    "device_brand": "MacBookPro17,1",
    "os_version": "15.7.4",
    # ids để trống — load_device() sinh/local lưu (id public repo hay bị shark)
    "device_id": "",
    "iid": "",
    "region": "VN",
    "loc": "VN",
    "lan": "vi-VN",
    "pf": "3",
    "tdid": "",
}

# device_id/iid từ K07VN demo — nhiều máy dùng chung → shark block
_BURNED_DEVICE_IDS = frozenset({"7647183892936328721", "7647185302080423697"})
_last_tts_create_at = 0.0
# 4 worker poll/download song song; chỉ stagger nhẹ lúc tạo task. Có thể chỉnh
# CAPCUT_TTS_CREATE_GAP nếu tài khoản/device bị rate-limit.
_MIN_CREATE_GAP_S = max(0.2, float(os.environ.get("CAPCUT_TTS_CREATE_GAP", "0.65")))
_create_lock = threading.Lock()

TTS_SIGN_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAmTd34Lw4b7IuldSXh/zY
CMla+ITdGG5TeWz6ad+OySd4r+IrY45AoqrYUxhQ2dl+7z+i7r/5vEa8rr39BYfB
8AGMQLmZA8HmgpWBsqrn/V6daUALkKnkLb70Fn32CJigIuGXAYqxUdGuI340aC+0
v5Es3puJsHyzf01/AelE4Cdc6bZhQrASJLBh8R3BQToYClmDVSDUQk28o8sl/guA
Z4n303Vj+6Siv1HayPCdV6kpVVnMBAG4+umUbwGmn132N3fgpzLarFF3XyWmS1zh
D/J07iM/rP8GDO9IskHNHd2phrO0G6KzrcFAnTBHjVv+hCBEfzN/no3FNA9AuC36
mwIDAQAB
-----END PUBLIC KEY-----"""


def compact_json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def make_x_ss_stub(body_text):
    return hashlib.md5(body_text.encode("utf-8")).hexdigest()

def _der_len(data, pos):
    first = data[pos]
    pos += 1
    if first < 0x80:
        return first, pos
    nbytes = first & 0x7F
    return int.from_bytes(data[pos : pos + nbytes], "big"), pos + nbytes

def _der_value(data, pos, tag):
    if data[pos] != tag:
        raise ValueError(f"bad DER tag: expected 0x{tag:02x}, got 0x{data[pos]:02x}")
    length, pos = _der_len(data, pos + 1)
    return data[pos : pos + length], pos + length

def _der_int(data, pos):
    raw, pos = _der_value(data, pos, 0x02)
    return int.from_bytes(raw.lstrip(b"\x00"), "big"), pos

def rsa_public_numbers_from_pem(pem):
    b64 = "".join(line for line in pem.splitlines() if not line.startswith("-----"))
    der = base64.b64decode(b64)
    outer, pos = _der_value(der, 0, 0x30)
    if pos != len(der):
        raise ValueError("trailing data in public key")
    _, pos = _der_value(outer, 0, 0x30)  # AlgorithmIdentifier
    bit_string, pos = _der_value(outer, pos, 0x03)
    if pos != len(outer) or not bit_string or bit_string[0] != 0:
        raise ValueError("bad subjectPublicKeyInfo")
    rsa_seq, pos = _der_value(bit_string[1:], 0, 0x30)
    if pos != len(bit_string[1:]):
        raise ValueError("trailing data in RSA public key")
    modulus, pos = _der_int(rsa_seq, 0)
    exponent, pos = _der_int(rsa_seq, pos)
    if pos != len(rsa_seq):
        raise ValueError("trailing integer data in RSA public key")
    return modulus, exponent

def rsa_encrypt_pkcs1v15(message, pem=TTS_SIGN_PUBLIC_KEY_PEM):
    modulus, exponent = rsa_public_numbers_from_pem(pem)
    key_len = (modulus.bit_length() + 7) // 8
    msg = message.encode("utf-8") if isinstance(message, str) else bytes(message)
    if len(msg) > key_len - 11:
        raise ValueError("message too long for RSA PKCS#1 v1.5")
    ps_len = key_len - len(msg) - 3
    ps = bytearray()
    while len(ps) < ps_len:
        chunk = secrets.token_bytes(ps_len - len(ps))
        ps.extend(b for b in chunk if b != 0)
    encoded = b"\x00\x02" + bytes(ps[:ps_len]) + b"\x00" + msg
    encrypted = pow(int.from_bytes(encoded, "big"), exponent, modulus).to_bytes(key_len, "big")
    return base64.b64encode(encrypted).decode("ascii")

def make_tts_payload_sign(ssml, extra_info, device_id, app_id):
    ssml_md5 = hashlib.md5(ssml.encode("utf-8")).hexdigest()
    sign_input = f"appid:{app_id}&did:{device_id}&creditDisable:false&ssml:{ssml_md5}"
    if extra_info is not None:
        sign_input += f"&extraInfo:{extra_info}"
    return rsa_encrypt_pkcs1v15(sign_input)

def make_sign_header(url, appvr, device_time, tdid):
    path = url.split("?", 1)[0]
    sign_str = f"9e2c|{path[-7:]}|3|{appvr}|{device_time}|{tdid}|11ac"
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest()

def common_query(device, babi_param=None, include_region=True):
    q = {
        "app_name": device["app_name"],
        "device_type": device["device_type"],
        "os_version": device["os_version"],
        "channel": device["channel"],
        "version_name": device["version_name"],
        "device_brand": device["device_brand"],
        "device_id": device["device_id"],
        "iid": device["iid"],
        "version_code": device["version_code"],
        "device_platform": device["device_platform"],
        "aid": device["aid"],
    }
    if include_region:
        q["region"] = device["region"]
    if babi_param is not None:
        q["babi_param"] = compact_json(babi_param)
    return q


def base_headers(device, body_text, appid=False):
    now = str(int(time.time()))
    headers = {
        "content-type": "application/json",
        "appvr": device["appvr"],
        "ch": device["channel"],
        "device-time": now,
        "lan": device["lan"],
        "loc": device["loc"],
        "pf": device["pf"],
        "sign-ver": "1",
        "tdid": device["tdid"],
        "x-ss-stub": make_x_ss_stub(body_text),
        "x-ss-dp": device["aid"],
        "x-khronos": now,
        "x-tt-trace-id": make_trace_id(),
        "user-agent": "Cronet/TTNetVersion:1d7cc3b1 2025-07-16 QuicVersion:52c2b40d 2025-04-03",
        "accept-encoding": "gzip, deflate",
        "store-country-code": device["loc"].lower(),
        "store-country-code-src": "did",
        "is-dispatch-us-ttp": "0",
        "is-app-region-us-ttp": "0",
    }
    if appid:
        headers["app-sdk-version"] = device["appvr"]
        headers["appid"] = device["aid"]
    return headers


def make_trace_id():
    seed = uuid.uuid4().hex[:32]
    return f"00-{seed}-{seed[:16]}-01"


def tts_new_body(texts, voice, resource_id, rate, device):
    babi = {
        "feature_entrance": "editor",
        "feature_entrance_detail": "editor-feature-text_to_speech",
        "feature_key": "text_to_speech",
        "scenario": "video_editor",
    }
    voice_blocks = []
    for text in texts:
        voice_blocks.append(
            f'    <voice name="{voice}" mock_tone_info="" platform="sami" '
            f'resource_id="{resource_id}" emotion="" emotion_scale="0" style="" role="" '
            f'moyin_emotion="" is_clone_tone="false" need_subtitle_timestamp="false">\n'
            f'        <prosody rate="{rate}">{escape_xml(text)}</prosody>\n'
            f'    </voice>'
        )
    ssml = (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">\n'
        + "\n".join(voice_blocks)
        + "\n</speak>"
    )
    extra_info = compact_json({"benefit_info": {}})
    payload = {
        "audio_format": "mp3",
        "babi_param": compact_json(babi),
        "credit_disable": False,
        "extra_info": extra_info,
        "need_merge_voice": False,
        "need_subtitle_timestamp": False,
        "scene": "text_to_speech",
        "ssml": ssml,
    }
    payload["sign"] = make_tts_payload_sign(ssml, extra_info, device["device_id"], device["aid"])
    body = {
        "bind_id": str(uuid.uuid4()),
        "can_queue": True,
        "enter_from": "text_to_speech",
        "tasks": [
            {
                "context": str(uuid.uuid4()),
                "payload": compact_json(payload),
                "req_key": "sami_text_to_speech",
                "task_version": "v3",
            }
        ],
    }
    return babi, body


def query_body(task_id, token, req_key, bind_id=""):
    return {"tasks": [{"bind_id": bind_id, "id": task_id, "req_key": req_key, "task_version": "v3", "token": token}]}


def escape_xml(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )



def _http(**kwargs: Any) -> httpx.Client:
    kwargs.setdefault("trust_env", False)
    return httpx.Client(**kwargs)


def _device_path() -> Path:
    path = os.environ.get("CAPCUT_DEVICE_JSON", "").strip()
    if path:
        return Path(path)
    return Path(__file__).resolve().parents[2] / "capcut_device.json"


def _rand_did() -> str:
    # ~19 chữ số kiểu CapCut did
    return str(10**18 + secrets.randbelow(9 * 10**18))


def _rotate_device_ids(device: dict[str, Any]) -> dict[str, Any]:
    out = dict(device)
    did = _rand_did()
    out["device_id"] = did
    out["tdid"] = did
    out["iid"] = _rand_did()
    return out


def _save_device(device: dict[str, Any]) -> None:
    p = _device_path()
    payload = {k: device.get(k, v) for k, v in DEFAULT_DEVICE.items()}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_device() -> dict[str, Any]:
    """Load device; tự mint id local nếu thiếu / trùng id demo bị shark."""
    device = deepcopy(DEFAULT_DEVICE)
    p = _device_path()
    if p.is_file():
        try:
            device.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    did = str(device.get("device_id") or "")
    iid = str(device.get("iid") or "")
    if (
        not did
        or not iid
        or did in _BURNED_DEVICE_IDS
        or iid in _BURNED_DEVICE_IDS
    ):
        device = _rotate_device_ids(device)
        _save_device(device)
    return device


def _throttle_create() -> None:
    global _last_tts_create_at
    # Nhiều worker được phép poll/download song song, nhưng thời điểm tạo task
    # vẫn phải cách nhau để CapCut không shark/rate-limit device.
    with _create_lock:
        gap = _MIN_CREATE_GAP_S - (time.time() - _last_tts_create_at)
        if gap > 0:
            time.sleep(gap)
        _last_tts_create_at = time.time()


def _is_shark(err: BaseException | str) -> bool:
    s = str(err).lower()
    return "shark" in s or "ret=-6" in s


def _post_json(path: str, body: dict[str, Any], device: dict[str, Any], *, babi: dict | None, appid: bool) -> dict[str, Any]:
    body_text = compact_json(body)
    query = common_query(device, babi, include_region=babi is not None)
    url = BASE + path + "?" + urlencode(query)
    headers = base_headers(device, body_text, appid=appid)
    if "sign" not in {k.lower() for k in headers}:
        headers["sign"] = make_sign_header(url, device["appvr"], headers["device-time"], device["tdid"])
    last_err: Exception | None = None
    r = None
    for attempt in range(4):
        try:
            with _http(timeout=60.0) as client:
                r = client.post(url, headers=headers, content=body_text.encode("utf-8"))
            break
        except Exception as e:
            last_err = e
            if attempt < 3:
                import time
                time.sleep(0.5 * (attempt + 1))
                continue
            raise RuntimeError(f"CapCut network error after 4 attempts: {e}") from e
    if r is None:
        raise RuntimeError(f"CapCut request failed: {last_err}")
    try:
        data = r.json()
    except Exception as e:
        raise RuntimeError(f"CapCut non-JSON HTTP {r.status_code}: {r.text[:400]}") from e
    ret = str(data.get("ret") or data.get("code") or "")
    errmsg = str(data.get("errmsg") or data.get("message") or "")
    if ret in ("-6", "6") or "shark" in errmsg.lower():
        raise RuntimeError("CapCut chặn (shark/ret=-6) — giảm tần suất hoặc đổi device.json")
    if r.status_code >= 400:
        raise RuntimeError(f"CapCut HTTP {r.status_code}: {data}")
    if ret and ret not in ("0", "00", ""):
        raise RuntimeError(f"CapCut lỗi ret={ret}: {errmsg or data}")
    return data


def _normalize_tts_text(text: str) -> str:
    """Normalize text forms that CapCut rejects as TTSInvalidText."""
    value = " ".join((text or "").replace("\ufffd", "").split()).strip()
    # OCR/subtitle extraction may put a space between every Chinese character;
    # CapCut's minor-language router can reject that form. Chinese text does
    # not need word separators, so collapse only spaces between adjacent CJK
    # characters while preserving spaces around Latin words and numbers.
    value = re.sub(r"(?<=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])\s+(?=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])", "", value)
    laugh = re.fullmatch(r"(?i)(ha){2,}[.!?…]*", value)
    if laugh:
        count = len(laugh.group(0).rstrip(".!?…")) // 2
        return "Ha."
    spaced_laugh = re.fullmatch(r"(?i)ha(?:[\s-]+ha){1,}[.!?…]*", value)
    if spaced_laugh:
        parts = re.findall(r"(?i)ha", spaced_laugh.group(0))
        return "Ha."
    return value or "."


def tts_create(text: str, voice: str, resource_id: str, rate: str = "1.0", device: dict[str, Any] | None = None) -> tuple[str, str]:
    device = device or load_device()
    _throttle_create()
    babi, body = tts_new_body([_normalize_tts_text(text)], voice, resource_id, rate, device)
    data = _post_json("/lv/v1/common_task/new", body, device, babi=babi, appid=True)
    tasks = ((data.get("data") or {}).get("tasks")) or []
    if not tasks:
        raise RuntimeError(f"CapCut TTS không trả task: {data}")
    task = tasks[0]
    tid, token = task.get("id"), task.get("token")
    if not tid or not token:
        raise RuntimeError(f"CapCut TTS thiếu id/token: {task}")
    return str(tid), str(token)


def tts_query(task_id: str, token: str, device: dict[str, Any] | None = None) -> dict[str, Any]:
    device = device or load_device()
    body = query_body(task_id, token, "sami_text_to_speech")
    return _post_json("/lv/v1/common_task/query", body, device, babi=None, appid=True)


_URL_RE = re.compile(r"https?://[^\s\"\']+\.(?:mp3|wav|m4a)(?:\?[^\s\"\']*)?", re.I)


def _find_audio_url(obj: Any) -> str | None:
    if isinstance(obj, str):
        if obj.startswith("http") and (".mp3" in obj.lower() or ".wav" in obj.lower() or "audio" in obj.lower()):
            m = _URL_RE.search(obj) or (obj if obj.startswith("http") else None)
            return m.group(0) if hasattr(m, "group") else (obj if isinstance(m, str) else None)
        try:
            return _find_audio_url(json.loads(obj))
        except (json.JSONDecodeError, TypeError):
            m = _URL_RE.search(obj)
            return m.group(0) if m else None
    if isinstance(obj, dict):
        for key in ("audio_url", "audioUrl", "url", "speak_url", "preview_url", "tos_url"):
            v = obj.get(key)
            if isinstance(v, str) and v.startswith("http"):
                return v
        for v in obj.values():
            u = _find_audio_url(v)
            if u:
                return u
    if isinstance(obj, list):
        for v in obj:
            u = _find_audio_url(v)
            if u:
                return u
    return None


def synthesize_mp3(text: str, voice: str, resource_id: str, out_mp3: Path, *, rate: str = "1.0", timeout_s: float = 60.0) -> Path:
    """Create CapCut TTS task, poll, download mp3. Shark → xoay device_id rồi thử lại."""
    last_err: BaseException | None = None
    for attempt in range(3):
        device = load_device()
        try:
            tid, token = tts_create(text, voice, resource_id, rate, device)
            deadline = time.time() + timeout_s
            last: dict[str, Any] = {}
            url: str | None = None
            while time.time() < deadline:
                last = tts_query(tid, token, device)
                tasks = ((last.get("data") or {}).get("tasks")) or []
                task = tasks[0] if tasks else {}
                status = str(task.get("status") or "").lower()
                url = _find_audio_url(task) or _find_audio_url(last)
                if status in ("failed", "fail", "error", "cancelled"):
                    sent_text = _normalize_tts_text(text)
                    raise RuntimeError(f"CapCut TTS thất bại (text={sent_text!r}): {task}")
                if url and status not in ("queueing", "processing", "running", "pending", "created"):
                    break
                if url and status in ("success", "succeed", "done", "finished", "complete", "completed", ""):
                    break
                time.sleep(0.8)
            else:
                raise RuntimeError(f"CapCut TTS timeout: {last}")

            if not url:
                url = _find_audio_url(((last.get("data") or {}).get("tasks") or [{}])[0]) or _find_audio_url(last)
            if not url:
                raise RuntimeError(f"CapCut TTS xong nhưng không thấy URL audio: {last}")

            out_mp3.parent.mkdir(parents=True, exist_ok=True)
            with _http(timeout=120.0) as client:
                r = client.get(url)
                r.raise_for_status()
                out_mp3.write_bytes(r.content)
            return out_mp3
        except Exception as e:
            last_err = e
            # CapCut can reject a laugh intermittently; retry once with a minimal
            # valid utterance instead of failing the whole dubbing job.
            if (
                "TTSInvalidText" in str(e)
                and attempt < 2
                and re.fullmatch(r"(?i)ha(?:[\s-]+ha)*[.!?…]*", " ".join((text or "").split()))
            ):
                text = "Ha."
                continue
            if attempt < 2 and not _is_shark(e):
                time.sleep(1.0 * (attempt + 1))
                continue
            if not _is_shark(e) or attempt >= 2:
                raise
            # ponytail: shark block — random device_id (upstream tip), chờ rồi thử lại
            fresh = _rotate_device_ids(load_device())
            _save_device(fresh)
            time.sleep(2.0 * (attempt + 1))
    assert last_err is not None
    raise last_err
