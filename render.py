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
    r = requests.get(url, timeout=180)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)


def post_with_retry(url: str, json_body: dict, timeout: int = 120, max_retries: int = 6):
    """429 gelirse exponential backoff ile tekrar dener."""
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
    """
    Gemini -> JSON sahneler:
    {"scenes":[{"image_prompt":"(EN) ...","narration":"(TR) ...","duration":6}]}
    """
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        # Key yoksa fallback (daha zayıf)
        scenes = []
        for i in range(scenes_count):
            scenes.append(
                {
                    "image_prompt": f"{style} cinematic film still. {prompt}. Scene {i+1}/{scenes_count}. "
                                   f"Highly detailed, consistent characters. Avoid: text, watermark, logo.",
                    "narration": prompt,
                    "duration": 6,
                }
            )
        return scenes

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"

    # Karakter tutarlılığı için "character bible" yaklaşımı:
    system = f"""
You are a storyboard generator.

TASK:
Given a Turkish user request, create a short video plan of exactly {scenes_count} scenes.

STYLE:
Overall visual style = {style}. Use cinematic film language (lighting, lens, composition).

OUTPUT FORMAT:
Return ONLY valid JSON, no markdown, no extra text:
{{
  "scenes": [
    {{
      "image_prompt": "...",   // ENGLISH. Very descriptive. Include character consistency details.
      "narration": "...",      // TURKISH. Natural narration for this scene.
      "duration": 6            // integer 4-8
    }}
  ]
}}

QUALITY RULES:
- Maintain the same main characters across scenes (appearance, clothing, names).
- Each image_prompt must specify: setting, time of day, lighting, camera vibe (film still), main characters, action, mood.
- Add an "Avoid:" clause at the end: "Avoid: text, watermark, logo, deformed anatomy, extra limbs, blurry faces".
- Keep prompts safe and family-friendly.
- Scenes must form a coherent story arc (start -> development -> twist/issue -> resolution).

USER REQUEST (Turkish):
{prompt}
""".strip()

    body = {"contents": [{"role": "user", "parts": [{"text": system}]}]}
    resp = post_with_retry(url, json_body=body, timeout=180, max_retries=6)
    txt = resp.json()["candidates"][0]["content"]["parts"][0]["text"]

    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.replace("```json", "").replace("```", "").strip()

    data = json.loads(txt)
    scenes = data.get("scenes", [])

    # Güvenlik: adet tutmazsa kırp/doldur
    if len(scenes) > scenes_count:
        scenes = scenes[:scenes_count]
    while len(scenes) < scenes_count:
        scenes.append(
            {
                "image_prompt": f"{style} cinematic film still. {prompt}. Extra scene. "
                                f"Avoid: text, watermark, logo, deformed anatomy, extra limbs.",
                "narration": prompt,
                "duration": 6,
            }
        )

    # duration clamp
    for s in scenes:
        try:
            d = int(s.get("duration", 6))
        except Exception:
            d = 6
        s["duration"] = max(4, min(8, d))

    return scenes


def tts_to_mp3(text: str, out_mp3: str, voice: str):
    sh(["edge-tts", "--voice", voice, "--text", text, "--write-media", out_mp3])


def ratio_to_dims(ratio: str):
    r = (ratio or "1x1").strip().lower()
    if r == "9x16":
        return 720, 1280
    if r == "16x9":
        return 1280, 720
    return 720, 720  # 1x1 default


def make_segment(img: str, mp3: str, out_mp4: str, out_w: int, out_h: int):
    # Oranı bozma -> scale + pad
    vf = (
        f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,"
        f"fps=30,format=yuv420p"
    )

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
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            out_mp4,
        ]
    )


def concat_segments_reencode(files: list[str], out_mp4: str):
    # Re-encode concat -> DTS problemlerini azaltır
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


def send_telegram(chat_id: str, video_path: str, caption: str = ""):
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    url = f"https://api.telegram.org/bot{token}/sendVideo"
    with open(video_path, "rb") as f:
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
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
    ratio = str(payload.get("ratio") or "1x1").strip()

    # Pollinations params
    model = str(payload.get("model") or "flux").strip()  # ör: flux, flux-realism, flux-anime
    enhance = str(payload.get("enhance") or "true").strip().lower()  # true/false
    seed_raw = payload.get("seed")
    try:
        seed = int(seed_raw) if str(seed_raw).strip() else random.randint(1, 10**9)
    except Exception:
        seed = random.randint(1, 10**9)

    scenes_raw = payload.get("scenes")
    try:
        scenes_count = int(scenes_raw) if str(scenes_raw).strip() else 6
    except Exception:
        scenes_count = 6
    scenes_count = max(1, min(12, scenes_count))

    out_w, out_h = ratio_to_dims(ratio)

    scenes = gemini_scenes(prompt, scenes_count=scenes_count, style=style)
    print(f"[Scenes] count={len(scenes)} style={style} voice={voice} ratio={ratio} model={model} seed={seed} enhance={enhance}")

    segments = []
    for i, sc in enumerate(scenes, start=1):
        ip = sc.get("image_prompt", prompt)
        nar = sc.get("narration", prompt)

        img = f"scene_{i}.jpg"
        mp3 = f"scene_{i}.mp3"
        mp4 = f"seg_{i}.mp4"

        # Pollinations URL with params :contentReference[oaicite:3]{index=3}
        q = {
            "width": out_w,
            "height": out_h,
            "model": model,
            "seed": seed + i,        # sahneler birbirinden ayrı ama deterministik
            "enhance": "true" if enhance == "true" else "false",
            "nologo": "true",
            "private": "true",
            "safe": "true",
        }
        img_url = POLLINATIONS + urllib.parse.quote(ip, safe="") + "?" + urllib.parse.urlencode(q)
        dl(img_url, img)

        tts_to_mp3(nar, mp3, voice=voice)
        make_segment(img, mp3, mp4, out_w, out_h)
        segments.append(mp4)

    out = "final.mp4"
    concat_segments_reencode(segments, out)

    caption = str(payload.get("caption") or "").strip()
    send_telegram(chat_id, out, caption=caption)


if __name__ == "__main__":
    main()
