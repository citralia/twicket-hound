import time
import random
import re
import os
import requests
import webbrowser
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# === Load env vars ===
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TEST_MODE = os.getenv("TEST_MODE", "False") == "True"
OPEN_BROWSER = os.getenv("OPEN_BROWSER", "False") == "True"
CHECK_INTERVAL = (int(os.getenv("CHECK_INTERVAL_MIN", 10)), int(os.getenv("CHECK_INTERVAL_MAX", 20)))

# Twickets Event URL
URL = "https://www.twickets.live/en/event/1869337143566929920#sort=FirstListed&typeFilter=Any&qFilter=1"

def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}
        )
    except Exception as e:
        print(f"[!] Telegram failed: {e}")

def start_browser():
    options = uc.ChromeOptions()
    options.headless = True
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    return uc.Chrome(options=options)

def check_for_tickets(driver):
    driver.get(URL)
    time.sleep(5)
    soup = BeautifulSoup(driver.page_source, "html.parser")
    listings = soup.select('div[data-testid="listing-card"]')

    found = []
    for listing in listings:
        if re.search(r'\b1\s+ticket\b', listing.get_text().lower()):
            a = listing.find("a", href=True)
            if a:
                found.append("https://www.twickets.live" + a["href"])
    return found

def test_mode_result():
    return ["https://www.twickets.live/simulated-ticket"]

def main():
    print("[*] Sniper running (Cloudflare-proof)...")
    driver = start_browser()

    while True:
        try:
            matches = test_mode_result() if TEST_MODE else check_for_tickets(driver)
            if matches:
                for match in matches:
                    print(f"[🎯] Ticket Found: {match}")
                    send_telegram(f"🎟️ <b>Single Ticket Found!</b>\n{match}")
                    if OPEN_BROWSER:
                        webbrowser.open(match)
                break

            sleep_time = random.uniform(*CHECK_INTERVAL)
            print(f"[-] No tickets. Sleeping {sleep_time:.1f}s...")
            time.sleep(sleep_time)

        except Exception as e:
            print(f"[!] Error: {e}")
            time.sleep(10)

    driver.quit()

if __name__ == "__main__":
    main()