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
STYLE_PRESETS = {
    "cinematic": "cinematic, ultra realistic, dramatic lighting, film still, shallow depth of field, 35mm",
    "documentary": "documentary photo, natural light, realistic, candid, photojournalism, sharp focus",
    "anime": "anime style, studio ghibli inspired, vibrant colors, clean line art, soft shading",
}



def sh(cmd: list[str]):
    print("RUN:", " ".join(cmd))
    subprocess.check_call(cmd)


def dl(url: str, path: str):
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)


def post_with_retry(url: str, json_body: dict, timeout: int = 120, max_retries: int = 6):
    """
    429 gelirse exponential backoff ile tekrar dener.
    """
    for attempt in range(max_retries):
        resp = requests.post(url, json=json_body, timeout=timeout)

        if resp.status_code != 429:
            resp.raise_for_status()
            return resp

        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            sleep_s = float(retry_after)
        else:
            sleep_s = min(60.0, (2 ** attempt) + random.uniform(0, 1.5))

        print(f"[Retry] 429 Rate limit. Sleep {sleep_s:.1f}s (attempt {attempt+1}/{max_retries})")
        time.sleep(sleep_s)

    resp.raise_for_status()  # en son da hata fırlatsın
    return resp


def gemini_scenes(prompt: str, scenes_count: int, style: str):
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        # Key yoksa fallback: aynı prompttan sahneler üret
        scenes = []
        for i in range(scenes_count):
            scenes.append(
                {
                    "image_prompt": f"{style} style. {prompt}. Scene {i+1}/{scenes_count}.",
                    "narration": prompt,
                    "duration": 6,
                }
            )
        return scenes

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"

    system = (
        f"Aşağıdaki kullanıcı isteğinden {scenes_count} sahnelik kısa bir video planı üret.\n"
        f"Genel görsel stil: {style}\n"
        "SADECE şu JSON formatında dön (başka hiçbir şey yazma):\n"
        '{"scenes":[{"image_prompt":"...","narration":"...","duration":6}]}\n'
        "Türkçe yaz. image_prompt görsel üretim için net ve betimleyici olsun.\n"
        "Her sahne için duration 4-8 saniye arası olabilir."
    )

    body = {
        "contents": [
            {"role": "user", "parts": [{"text": system + "\n\nKULLANICI:\n" + prompt}]}
        ]
    }

    resp = post_with_retry(url, json_body=body, timeout=120, max_retries=6)
    txt = resp.json()["candidates"][0]["content"]["parts"][0]["text"]

    # Model bazen ```json blokları ekleyebilir
    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.replace("```json", "").replace("```", "").strip()

    data = json.loads(txt)
    scenes = data["scenes"]

    # Güvenlik: adet tutmazsa kırp/doldur
    if len(scenes) > scenes_count:
        scenes = scenes[:scenes_count]
    while len(scenes) < scenes_count:
        scenes.append(
            {
                "image_prompt": f"{style} style. {prompt}. Extra scene.",
                "narration": prompt,
                "duration": 6,
            }
        )
    return scenes


def tts_to_mp3(text: str, out_mp3: str, voice: str):
    sh(["edge-tts", "--voice", voice, "--text", text, "--write-media", out_mp3])


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

    print("Telegram status:", r.status_code)
    if r.status_code != 200:
        print("Telegram response:", r.text[:800])
    r.raise_for_status()


def main():
    payload_path = sys.argv[1]
    payload = json.load(open(payload_path, "r", encoding="utf-8"))

    chat_id = str(payload["chat_id"])
    prompt = str(payload["text"])

    style = str(payload.get("style") or "cinematic").strip()
    style_key = style.lower()
    style_prompt = STYLE_PRESETS.get(style_key, style)

    voice = str(payload.get("voice") or "tr-TR-AhmetNeural").strip()

    scenes_raw = payload.get("scenes")
    try:
        scenes_count = int(scenes_raw) if str(scenes_raw).strip() else 6
    except Exception:
        scenes_count = 6
    scenes_count = max(1, min(12, scenes_count))

    scenes = gemini_scenes(prompt, scenes_count=scenes_count, style=style_prompt)
    print(f"[Scenes] count={len(scenes)} style={style} voice={voice}")

    segments = []
    for i, sc in enumerate(scenes, start=1):
        ip = sc.get("image_prompt", prompt)
        nar = sc.get("narration", prompt)

        img = f"scene_{i}.jpg"
        mp3 = f"scene_{i}.mp3"
        mp4 = f"seg_{i}.mp4"

        img_url = POLLINATIONS + urllib.parse.quote(ip, safe="")
        dl(img_url, img)

        tts_to_mp3(nar, mp3, voice=voice)
        make_segment(img, mp3, mp4)
        segments.append(mp4)

    out = "final.mp4"
    concat_segments(segments, out)
    send_telegram(chat_id, out)


if __name__ == "__main__":
    main()
