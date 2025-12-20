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

    resp.raise_for_status()
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

    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.replace("```json", "").replace("```", "").strip()

    data = json.loads(txt)
    scenes = data["scenes"]

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


def parse_ratio(ratio: str):
    """
    ratio: '1x1' | '9x16' | '16x9'
    Çıkış çözünürlüğü:
      - 1x1  -> 720x720
      - 9x16 -> 720x1280
      - 16x9 -> 1280x720
    """
    ratio = (ratio or "1x1").strip().lower()
    if ratio in ("1:1", "1x1", "square"):
        return 720, 720, "1x1"
    if ratio in ("9:16", "9x16", "vertical", "portrait"):
        return 720, 1280, "9x16"
    if ratio in ("16:9", "16x9", "horizontal", "landscape"):
        return 1280, 720, "16x9"
    # default
    return 720, 720, "1x1"


def make_segment(img: str, mp3: str, out_mp4: str, w: int, h: int):
    """
    Görseli hedef oranı dolduracak şekilde scale+crop yapar.
    """
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps=30"
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
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            out_mp4,
        ]
    )


def concat_segments(files: list[str], out_mp4: str, w: int, h: int):
    """
    Önce concat demuxer listesi oluşturur, sonra TEK DOSYADA yeniden encode eder.
    Böylece 'Non-monotonic DTS' gibi audio timestamp sorunları büyük ölçüde biter.
    """
    lst = "concat_" + uuid.uuid4().hex + ".txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in files:
            f.write(f"file '{p}'\n")

    sh(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            lst,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            out_mp4,
        ]
    )


def send_telegram(chat_id: str, video_path: str, caption: str = ""):
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    url = f"https://api.telegram.org/bot{token}/sendVideo"

    data = {"chat_id": chat_id}
    caption = (caption or "").strip()
    if caption:
        # Telegram caption max 1024; güvenli kırp
        data["caption"] = caption[:1024]

    with open(video_path, "rb") as f:
        r = requests.post(url, data=data, files={"video": f}, timeout=300)

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
    voice = str(payload.get("voice") or "tr-TR-AhmetNeural").strip()

    scenes_raw = payload.get("scenes")
    try:
        scenes_count = int(scenes_raw) if str(scenes_raw).strip() else 6
    except Exception:
        scenes_count = 6
    scenes_count = max(1, min(12, scenes_count))

    ratio = str(payload.get("ratio") or "1x1").strip()
    w, h, ratio_norm = parse_ratio(ratio)

    caption = str(payload.get("caption") or "").strip()

    scenes = gemini_scenes(prompt, scenes_count=scenes_count, style=style)
    print(f"[Scenes] count={len(scenes)} style={style} voice={voice} ratio={ratio_norm} size={w}x{h}")

    segments = []
    for i, sc in enumerate(scenes, start=1):
        ip = sc.get("image_prompt", prompt)
        nar = sc.get("narration", prompt)

        img = f"scene_{i}.jpg"
        mp3 = f"scene_{i}.mp3"
        mp4 = f"seg_{i}.mp4"

        # Oranı ipucu olarak prompt'a eklemek istersen (isteğe bağlı)
        # ip2 = f"{ip} -- aspect ratio {ratio_norm}"
        ip2 = ip

        img_url = POLLINATIONS + urllib.parse.quote(ip2, safe="")
        dl(img_url, img)

        tts_to_mp3(nar, mp3, voice=voice)
        make_segment(img, mp3, mp4, w=w, h=h)
        segments.append(mp4)

    out = "final.mp4"
    concat_segments(segments, out, w=w, h=h)
    send_telegram(chat_id, out, caption=caption)


if __name__ == "__main__":
    main()
