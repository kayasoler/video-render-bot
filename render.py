import json
import os
import random
import subprocess
import sys
import time
import urllib.parse
import uuid
import requests

POLLINATIONS = "https://image.pollinations.ai/prompt/"
VOICE = os.getenv("TTS_VOICE", "tr-TR-AhmetNeural")

# --------------------------
# Helpers
# --------------------------
def sh(cmd: list[str]):
    print("RUN:", " ".join(cmd), flush=True)
    subprocess.check_call(cmd)

def dl(url: str, path: str):
    r = requests.get(url, timeout=180)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)

def post_with_retry(url: str, json_body: dict, timeout: int = 120, max_retries: int = 8):
    """
    429 gelirse exponential backoff + jitter ile bekler.
    Denemeler biterse None döner (job fail etmesin, fallback çalışsın).
    """
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=json_body, timeout=timeout)
        except requests.RequestException as e:
            # network/timeout vs.
            if attempt == max_retries:
                print(f"[Retry] Network error, giving up: {e}", flush=True)
                return None
            sleep_s = min(60, (2 ** (attempt - 1)) + random.uniform(0, 1.5))
            print(f"[Retry] Network error. Sleep {sleep_s:.1f}s (attempt {attempt}/{max_retries})", flush=True)
            time.sleep(sleep_s)
            continue

        # Rate limit
        if resp.status_code == 429:
            if attempt == max_retries:
                print("[Retry] 429 Rate limit. Max retries reached -> fallback.", flush=True)
                return None
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                sleep_s = int(retry_after)
            else:
                sleep_s = min(60, (2 ** (attempt - 1)) + random.uniform(0, 1.5))
            print(f"[Retry] 429 Rate limit. Sleep {sleep_s:.1f}s (attempt {attempt}/{max_retries})", flush=True)
            time.sleep(sleep_s)
            continue

        # Other errors
        if resp.status_code >= 400:
            # Bazı 5xx durumlarında retry mantıklı
            if resp.status_code >= 500 and attempt < max_retries:
                sleep_s = min(30, (2 ** (attempt - 1)) + random.uniform(0, 1.5))
                print(f"[Retry] HTTP {resp.status_code}. Sleep {sleep_s:.1f}s (attempt {attempt}/{max_retries})", flush=True)
                time.sleep(sleep_s)
                continue

            print(f"[HTTP ERROR] {resp.status_code} -> {resp.text[:500]}", flush=True)
            return None

        return resp

    return None


# --------------------------
# Scene generation
# --------------------------
def fallback_scenes(prompt: str):
    """
    Gemini yoksa / 429 ise 6 sahnelik basit plan üret.
    """
    base = prompt.strip()
    if not base:
        base = "Kısa, duygusal bir Türkçe hikaye"

    # 6 sahne, her biri 6 sn
    return [
        {"image_prompt": f"{base}, sinematik geniş açı, 16:9, yüksek detay", "narration": f"{base} başlıyor.", "duration": 6},
        {"image_prompt": f"{base}, karakter yakın plan, dramatik ışık, 16:9", "narration": "Kahramanımız bir kararın eşiğine gelir.", "duration": 6},
        {"image_prompt": f"{base}, çevre detayı, sıcak tonlar, 16:9", "narration": "Yolculuk, küçük işaretlerle şekillenir.", "duration": 6},
        {"image_prompt": f"{base}, gerilimli an, sinematik, 16:9", "narration": "Beklenmedik bir engel ortaya çıkar.", "duration": 6},
        {"image_prompt": f"{base}, umut veren sahne, gün batımı, 16:9", "narration": "Cesaret, her şeyi değiştiren ana dönüşür.", "duration": 6},
        {"image_prompt": f"{base}, final sahnesi, huzurlu atmosfer, 16:9", "narration": "Ve hikaye, akılda kalan bir notayla biter.", "duration": 6},
    ]

def gemini_scenes(prompt: str):
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("[Gemini] GEMINI_API_KEY yok -> fallback", flush=True)
        return fallback_scenes(prompt)

    # Model adı istersen env ile değiştir:
    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    system = (
        "Aşağıdaki kullanıcı isteğinden 6 sahnelik kısa bir video planı üret.\n"
        "SADECE şu JSON formatında dön (başka hiçbir şey yazma):\n"
        '{"scenes":[{"image_prompt":"...","narration":"...","duration":6}]}\n'
        "Türkçe yaz. image_prompt görsel üretim için net ve sahneye uygun olsun.\n"
        "duration her sahne için 6 olsun.\n"
    )

    body = {
        "contents": [
            {"role": "user", "parts": [{"text": system + "\nKULLANICI:\n" + prompt}]}
        ]
    }

    resp = post_with_retry(url, json_body=body, timeout=120, max_retries=8)
    if resp is None:
        return fallback_scenes(prompt)

    try:
        data = resp.json()
        txt = data["candidates"][0]["content"]["parts"][0]["text"]
        txt = txt.strip()
        if txt.startswith("```"):
            # ```json ... ``` temizle
            txt = txt.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(txt)
        scenes = parsed.get("scenes")
        if not scenes or not isinstance(scenes, list):
            print("[Gemini] JSON format beklenenden farklı -> fallback", flush=True)
            return fallback_scenes(prompt)
        return scenes
    except Exception as e:
        print(f"[Gemini] Parse error -> fallback: {e}", flush=True)
        return fallback_scenes(prompt)


# --------------------------
# Media pipeline
# --------------------------
def tts_to_mp3(text: str, out_mp3: str):
    sh(["edge-tts", "--voice", VOICE, "--text", text, "--write-media", out_mp3])

def make_segment(img: str, mp3: str, out_mp4: str):
    sh([
        "ffmpeg", "-y",
        "-loop", "1", "-i", img,
        "-i", mp3,
        "-shortest",
        "-vf", "scale=720:-2,fps=30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out_mp4
    ])

def concat_segments(files: list[str], out_mp4: str):
    lst = "concat_" + uuid.uuid4().hex + ".txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in files:
            f.write(f"file '{p}'\n")

    # aynı codec ayarlarıyla üretildiği için copy genelde sorunsuz
    try:
        sh(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out_mp4])
    except subprocess.CalledProcessError:
        # bazen concat copy takılabilir -> reencode fallback
        sh(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            "-c:a", "aac", "-b:a", "128k",
            out_mp4])

def send_telegram(chat_id: str, video_path: str):
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    url = f"https://api.telegram.org/bot{token}/sendVideo"
    with open(video_path, "rb") as f:
        r = requests.post(
            url,
            data={"chat_id": chat_id},
            files={"video": f},
            timeout=600
        )
    r.raise_for_status()
    print("Telegram OK:", r.text[:300], flush=True)


# --------------------------
# Main
# --------------------------
def main():
    if len(sys.argv) < 2:
        print("Usage: python3 render.py payload.json")
        sys.exit(2)

    payload_path = sys.argv[1]
    with open(payload_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    chat_id = str(payload.get("chat_id", "")).strip()
    prompt = str(payload.get("text", "")).strip()

    if not chat_id:
        raise RuntimeError("payload.json içinde chat_id yok")
    if not prompt:
        prompt = "Kısa bir Türkçe hikaye"

    scenes = gemini_scenes(prompt)
    print(f"[Scenes] count={len(scenes)}", flush=True)

    segments = []
    for i, sc in enumerate(scenes, start=1):
        ip = str(sc.get("image_prompt", prompt))
        nar = str(sc.get("narration", prompt))

        img = f"scene_{i}.jpg"
        mp3 = f"scene_{i}.mp3"
        mp4 = f"seg_{i}.mp4"

        img_url = POLLINATIONS + urllib.parse.quote(ip, safe="")
        dl(img_url, img)
        tts_to_mp3(nar, mp3)
        make_segment(img, mp3, mp4)
        segments.append(mp4)

    out = "final.mp4"
    concat_segments(segments, out)
    send_telegram(chat_id, out)


if __name__ == "__main__":
    main()