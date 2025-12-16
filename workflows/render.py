import json, os, subprocess, sys, urllib.parse, requests, textwrap, uuid

POLLINATIONS = "https://image.pollinations.ai/prompt/"
VOICE = "tr-TR-AhmetNeural"  # istersen sonra değiştiririz

def sh(cmd: list[str]):
    print("RUN:", " ".join(cmd))
    subprocess.check_call(cmd)

def dl(url: str, path: str):
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)

def gemini_scenes(prompt: str):
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        # API key yoksa basit fallback: tek sahne
        return [{"image_prompt": prompt, "narration": prompt, "duration": 6}]

    # Resmi dokümantasyondaki "models.generateContent" yaklaşımıyla JSON üretelim. :contentReference[oaicite:4]{index=4}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    system = (
        "Aşağıdaki kullanıcı isteğinden 6 sahnelik kısa bir video planı üret.\n"
        "SADECE şu JSON formatında dön:\n"
        '{"scenes":[{"image_prompt":"...","narration":"...","duration":6}]}\n'
        "Türkçe yaz. image_prompt görsel üretim için net olsun."
    )
    body = {
        "contents": [
            {"role":"user","parts":[{"text": system + "\n\nKULLANICI:\n" + prompt}]}
        ]
    }
    resp = requests.post(url, json=body, timeout=120)
    resp.raise_for_status()
    txt = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    # Model bazen ```json blokları ekleyebilir; temizleyelim:
    txt = txt.strip().removeprefix("```json").removesuffix("```").strip()
    data = json.loads(txt)
    return data["scenes"]

def tts_to_mp3(text: str, out_mp3: str):
    sh(["edge-tts", "--voice", VOICE, "--text", text, "--write-media", out_mp3])

def make_segment(img: str, mp3: str, out_mp4: str):
    # 720p + hızlı preset: Telegram 50MB sınırına daha rahat girer. :contentReference[oaicite:5]{index=5}
    sh([
        "ffmpeg","-y",
        "-loop","1","-i", img,
        "-i", mp3,
        "-shortest",
        "-vf","scale=720:-2,fps=30",
        "-c:v","libx264","-preset","veryfast","-crf","30",
        "-c:a","aac","-b:a","128k",
        "-movflags","+faststart",
        out_mp4
    ])

def concat_segments(files: list[str], out_mp4: str):
    lst = "concat_" + uuid.uuid4().hex + ".txt"
    with open(lst,"w",encoding="utf-8") as f:
        for p in files:
            f.write(f"file '{p}'\n")
    sh(["ffmpeg","-y","-f","concat","-safe","0","-i",lst,"-c","copy",out_mp4])

def send_telegram(chat_id: str, video_path: str):
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    url = f"https://api.telegram.org/bot{token}/sendVideo"
    with open(video_path,"rb") as f:
        r = requests.post(url, data={"chat_id": chat_id}, files={"video": f}, timeout=300)
    r.raise_for_status()
    print("Telegram response:", r.text[:500])

def main():
    payload_path = sys.argv[1]
    payload = json.load(open(payload_path,"r",encoding="utf-8"))
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
