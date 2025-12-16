import json
import os
import subprocess
import sys
import urllib.parse
import uuid
import time
import random
import re
import requests

POLLINATIONS = "https://image.pollinations.ai/prompt/"
VOICE = "tr-TR-AhmetNeural"


def sh(cmd: list[str]):
    print("RUN:", " ".join(cmd))
    subprocess.check_call(cmd)


def post_with_retry(url, headers=None, payload=None, json_body=None, timeout=60, max_retries=6):
    """
    429 (rate limit) ve geçici 5xx hatalarında exponential backoff ile tekrar dener.
    """
    headers = headers or {}
    for attempt in range(max_retries):
        resp = requests.post(url, headers=headers, data=payload, json=json_body, timeout=timeout)

        # Başarılı veya 4xx (429 hariç) ise direkt dön/hata ver
        if resp.status_code != 429 and resp.status_code < 500:
            resp.raise_for_status()
            return resp

        # 5xx veya 429 -> retry
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                sleep_s = float(retry_after)
            else:
                sleep_s = min(60.0, (2 ** attempt) + random.uniform(0, 1.5))
            print(f"[Retry] 429 Rate limit. Sleep {sleep_s:.1f}s (attempt {attempt+1}/{max_retries})")
            time.sleep(sleep_s)
            continue

        # 5xx
        sleep_s = min(60.0, (2 ** attempt) + random.uniform(0, 1.5))
        print(f"[Retry] {resp.status_code} Server error. Sleep {sleep_s:.1f}s (attempt {attempt+1}/{max_retries})")
        time.sleep(sleep_s)

    # Hala olmadıysa son cevabı patlat
    resp.raise_for_status()
    return resp


def dl(url: str, path: str, timeout=120, max_retries=5):
    """
    Görsel indirme için basit retry.
    """
    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            with open(path, "wb") as f:
                f.write(r.content)
            return
        except Exception as e:
            last_err = e
            sleep_s = min(30.0, (2 ** attempt) + random.uniform(0, 1.0))
            print(f"[DL] download failed: {e} -> sleep {sleep_s:.1f}s (attempt {attempt+1}/{max_retries})")
            time.sleep(sleep_s)
    raise last_err


def _extract_json(text: str) -> dict:
    """
    Gemini bazen ```json ... ``` döndürebilir veya JSON etrafına açıklama koyabilir.
    En iyi ihtimal: direkt json.loads
    Olmazsa: ilk { ... } bloğunu yakalamayı dener.
    """
    t = text.strip()

    # code fence temizle
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()

    try:
        return json.loads(t)
    except Exception:
        pass

    # İlk büyük JSON objesini yakala
    m = re.search(r"\{.*\}", t, flags=re.DOTALL)
    if m:
        return json.loads(m.group(0))

    raise ValueError("Model çıktısından JSON çıkarılamadı.")


def gemini_scenes(prompt: str):
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        # API key yoksa basit fallback: tek sahne
        return [{"image_prompt": prompt, "narration": prompt, "duration": 6}]

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"

    system = (
        "Aşağıdaki kullanıcı isteğinden 6 sahnelik kısa bir video planı üret.\n"
        "SADECE şu JSON formatında dön (başka metin ekleme):\n"
        '{"scenes":[{"image_prompt":"...","narration":"...","duration":6}]}\n'
        "Türkçe yaz. image_prompt görsel üretim için net ve kısa olsun.\n"
        "duration saniye cinsinden 5-7 arası olabilir.\n"
    )

    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": system + "\nKULLANICI:\n" + prompt}],
            }
        ]
    }

    resp = post_with_retry(url, json_body=body, timeout=120, max_retries=6)
    data = resp.json()

    txt = data["candidates"][0]["content"]["parts"][0]["text"]
    parsed = _extract_json(txt)

    scenes = parsed.get("scenes")
    if not scenes or not isinstance(scenes, list):
        # beklenmedik format -> fallback
        return [{"image_prompt": prompt, "narration": prompt, "duration": 6}]

    # Minimum alan kontrolü
    cleaned = []
    for sc in scenes[:6]:
        cleaned.append(
            {
                "image_prompt": str(sc.get("image_prompt", prompt)).strip(),
                "narration": str(sc.get("narration", prompt)).strip(),
                "duration": int(sc.get("duration", 6)),
            }
        )
    return cleaned


def tts_to_mp3(text: str, out_mp3: str):
    sh(["edge-tts", "--voice", VOICE, "--text", text, "--write-media", out_mp3])


def make_segment(img: str, mp3: str, out_mp4: str):
    sh(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            img,
            "-i",
            mp3,
            "-shortest",
            "-vf",
            "scale=720:-2,fps=30",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            out_mp4,
        ]
    )


def concat_segments(files: list[str], out_mp4: str):
    lst = "concat_" + uuid.uuid4().hex + ".txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in files:
            f.write(f"file '{p}'\n")
    sh(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out_mp4])


def send_telegram(chat_id: str, video_path: str):
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    url = f"https://api.telegram.org/bot{token}/sendVideo"
    with open(video_path, "rb") as f:
        r = requests.post(url, data={"chat_id": chat_id}, files={"video": f}, timeout=300)
    r.raise_for_status()
    print("Telegram response:", r.text[:500])


def main():
    payload_path = sys.argv[1]
    payload = json.load(open(payload_path, "r", encoding="utf-8"))

    chat_id = str(payload["chat_id"])
    prompt = payload["text"]

    scenes = gemini_scenes(prompt)
    segments = []

    for i, sc in enumerate(scenes, start=1):
        ip = sc["image_prompt"]
        nar = sc["narration"]

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
