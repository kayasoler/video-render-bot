import json
import os
import random
import subprocess
import sys
import time
import urllib.parse

import requests

POLLINATIONS = "https://image.pollinations.ai/prompt/"
VOICE = os.getenv("VOICE", "tr-TR-AhmetNeural")


def sh(cmd: list[str]):
    print("RUN:", " ".join(cmd))
    subprocess.check_call(cmd)


def dl(url: str, path: str):
    r = requests.get(url, timeout=180)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)


def post_with_retry(url: str, json_body: dict, headers: dict | None = None, timeout: int = 120, max_retries: int = 8):
    """
    429 ve geçici hatalarda exponential backoff + jitter uygular.
    Eğer Retry-After varsa onu dikkate alır.
    """
    for attempt in range(1, max_retries + 1):
        resp = requests.post(url, json=json_body, headers=headers, timeout=timeout)

        # Başarılıysa dön
        if 200 <= resp.status_code < 300:
            return resp

        # 429 / 5xx -> retry
        if resp.status_code == 429 or (500 <= resp.status_code < 600):
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    sleep_s = float(retry_after)
                except ValueError:
                    sleep_s = 0
            else:
                # 1.5, 3, 6, 12 ... gibi büyüsün, üst sınır 90sn
                sleep_s = min(90.0, (1.5 * (2 ** (attempt - 1))) + random.uniform(0, 1.5))

            print(f"[Retry] HTTP {resp.status_code}. Sleep {sleep_s:.1f}s (attempt {attempt}/{max_retries})")
            time.sleep(sleep_s)
            continue

        # Diğer hata -> direkt patlat
        resp.raise_for_status()

    # Buraya gelirse tüm retry’lar bitti
    resp.raise_for_status()


def _extract_json_text(model_text: str) -> str:
    """
    Model bazen ```json ... ``` döndürebilir.
    Bazen de fazladan açıklama ekler. İlk JSON objesini ayıklamaya çalışır.
    """
    t = model_text.strip()

    if t.startswith("```"):
        t = t.replace("```json", "").replace("```", "").strip()

    # Eğer hala etrafında yazı varsa ilk { ile son } arasını almayı dene
    first = t.find("{")
    last = t.rfind("}")
    if first != -1 and last != -1 and last > first:
        return t[first:last + 1]

    return t


def fallback_scenes(prompt: str):
    # Gemini yoksa / 429 yüzünden çalışmazsa en azından workflow devam etsin
    return [
        {"image_prompt": f"Kapadokya manzarası, sinematik, gün doğumu, gerçekçi, yüksek detay", "narration": prompt, "duration": 6},
        {"image_prompt": f"Peri bacaları arasında yürüyen bir gezgin, sinematik, gerçekçi", "narration": "Peri bacalarının gölgesinde kısa bir yolculuk başlar.", "duration": 6},
        {"image_prompt": f"Sıcak hava balonları gökyüzünde, Kapadokya, geniş açı, sinematik", "narration": "Gökyüzü balonlarla dolarken rüzgâr hikâyeyi fısıldar.", "duration": 6},
        {"image_prompt": f"Taş bir sokak, eski Kapadokya evi, akşam ışığı, sinematik", "narration": "Dar sokaklarda adımlar yankılanır, zaman yavaşlar.", "duration": 6},
        {"image_prompt": f"Mağara otel içi, sıcak ışıklar, rahat atmosfer, sinematik", "narration": "Sıcak bir ışık altında hikâye tamamlanmaya yaklaşır.", "duration": 6},
        {"image_prompt": f"Kapadokya gün batımı, turuncu gökyüzü, siluetler, sinematik", "narration": "Gün batarken Kapadokya, anıyı sessizce mühürler.", "duration": 6},
    ]


def gemini_scenes(prompt: str):
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("[Gemini] GEMINI_API_KEY yok. Fallback sahneler kullanılacak.")
        return fallback_scenes(prompt)

    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    system = (
        "Kullanıcı isteğinden 6 sahnelik kısa bir video planı üret.\n"
        "Sadece ve sadece aşağıdaki JSON formatında cevap ver (başka hiçbir şey yazma):\n"
        '{"scenes":[{"image_prompt":"...","narration":"...","duration":6}]}\n'
        "Dil: Türkçe.\n"
        "image_prompt: görsel üretim için net, sinematik, gerçekçi betimleme.\n"
        "narration: kısa ve akıcı.\n"
        "duration: her sahne için 5-7 arası bir sayı kullan.\n"
    )

    body = {
        "contents": [
            {"role": "user", "parts": [{"text": system + "\nKULLANICI:\n" + prompt}]}
        ]
    }

    try:
        resp = post_with_retry(url, json_body=body, timeout=120, max_retries=8)
        data = resp.json()

        txt = data["candidates"][0]["content"]["parts"][0]["text"]
        txt = _extract_json_text(txt)
        parsed = json.loads(txt)
        scenes = parsed.get("scenes", [])
        if not scenes:
            print("[Gemini] JSON geldi ama scenes boş. Fallback kullanılacak.")
            return fallback_scenes(prompt)

        # duration normalize
        out = []
        for sc in scenes[:6]:
            out.append({
                "image_prompt": str(sc.get("image_prompt", prompt)),
                "narration": str(sc.get("narration", prompt)),
                "duration": int(sc.get("duration", 6)) if str(sc.get("duration", "")).isdigit() else 6,
            })
        while len(out) < 6:
            out.append({"image_prompt": prompt, "narration": prompt, "duration": 6})
        return out

    except requests.exceptions.HTTPError as e:
        # 429 vs. vs. -> fallback ile devam et
        print(f"[Gemini] Hata: {e}. Fallback sahneler kullanılacak.")
        return fallback_scenes(prompt)
    except Exception as e:
        print(f"[Gemini] Beklenmeyen hata: {e}. Fallback sahneler kullanılacak.")
        return fallback_scenes(prompt)


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
    lst = "concat_list.txt"
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
    # küçük jitter: aynı anda çok run olursa 429 ihtimali düşer
    time.sleep(random.uniform(0.2, 1.2))

    payload_path = sys.argv[1]
    with open(payload_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    chat_id = str(payload["chat_id"])
    prompt = str(payload["text"])

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
