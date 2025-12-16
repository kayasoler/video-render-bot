import time
import random
import requests

def post_with_retry(url, headers, payload, max_retries=6):
    for attempt in range(max_retries):
        resp = requests.post(url, headers=headers, json=payload, timeout=60)

        if resp.status_code != 429:
            resp.raise_for_status()
            return resp

        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            sleep_s = int(retry_after)
        else:
            sleep_s = min(60, (2 ** attempt) + random.uniform(0, 1.5))

        print(f"[Gemini] 429 rate limit. Sleeping {sleep_s:.1f}s then retrying... (attempt {attempt+1}/{max_retries})")
        time.sleep(sleep_s)

    resp.raise_for_status()
