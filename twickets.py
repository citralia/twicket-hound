import os
import time
import random
import logging
import pickle
from datetime import datetime, timedelta, date
import requests
from dotenv import load_dotenv
import signal
import re
import html
import asyncio
from shutil import which

from selenium.common.exceptions import TimeoutException, WebDriverException
from requests.exceptions import ReadTimeout, ConnectionError
from urllib3.exceptions import ReadTimeoutError

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
import undetected_chromedriver as uc

# Load environment variables
load_dotenv(override=True)

# Ensure logs and data directories exist (place after load_dotenv)
os.makedirs('./app/logs/', exist_ok=True)
os.makedirs('./app/data/', exist_ok=True)

chrome_bin = os.environ.get("CHROME_BIN", "/usr/bin/chromium")

# Configuration from environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID").split(",") if os.getenv("CHAT_ID") else []
EVENT_URL = os.getenv("EVENT_URL")
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"
HEARTBEAT_INTERVAL_MINUTES = int(os.getenv("HEARTBEAT_INTERVAL_MINUTES", 60))  # Increased to 60
SLEEP_MIN = int(os.getenv("SLEEP_MIN", 30))  # Increased to 30
SLEEP_MAX = int(os.getenv("SLEEP_MAX", 60))  # Increased to 60
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 3))
DRIVER_RESTART_INTERVAL = int(os.getenv("DRIVER_RESTART_INTERVAL", 200))  # Increased to 200
RATE_LIMIT_RESTART_THRESHOLD = int(os.getenv("RATE_LIMIT_RESTART_THRESHOLD", 3))
RATE_LIMIT_PAUSE_SECONDS = int(os.getenv("RATE_LIMIT_PAUSE_SECONDS", 300))  # Reduced to 300
last_ticket_results = None
last_message_time = None
RESEND_INTERVAL_HOURS = 4

# Logging setup with memory buffer
from logging.handlers import RotatingFileHandler
log_handler = RotatingFileHandler("./app/logs/twickets.log", maxBytes=2*1024*1024, backupCount=2)  # Reduced size
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    handlers=[log_handler, logging.StreamHandler()]
)
logger = logging.getLogger()

# Stats and timing globals
tickets_spotted = 0
error_count = 0
rate_limit_count = 0
last_summary_time = datetime.now()
current_day = date.today()
COOKIE_FILE = "/app/data/twickets_cookies.pkl"
cookies_cache = None  # In-memory cookie cache

def validate_env_vars():
    required_vars = ["TELEGRAM_BOT_TOKEN", "CHAT_ID"]
    missing = [var for var in required_vars if not os.getenv(var)]
    if missing:
        logger.error(f"Missing environment variables: {', '.join(missing)}")
        raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")
    logger.info(f"Loaded environment variables: EVENT_URL={EVENT_URL}, TEST_MODE={TEST_MODE}, "
                f"HEARTBEAT_INTERVAL_MINUTES={HEARTBEAT_INTERVAL_MINUTES}, SLEEP_MIN={SLEEP_MIN}, "
                f"SLEEP_MAX={SLEEP_MAX}, MAX_RETRIES={MAX_RETRIES}, DRIVER_RESTART_INTERVAL={DRIVER_RESTART_INTERVAL}, "
                f"RATE_LIMIT_RESTART_THRESHOLD={RATE_LIMIT_RESTART_THRESHOLD}, "
                f"RATE_LIMIT_PAUSE_SECONDS={RATE_LIMIT_PAUSE_SECONDS}")

async def send_telegram_message(text, retries=MAX_RETRIES, backoff=5):
    async def send_to_chat(chat_id):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id.strip(), "text": text, "parse_mode": "HTML"}
        for attempt in range(1, retries + 1):
            try:
                resp = requests.post(url, data=payload, timeout=5)  # Reduced timeout
                resp.raise_for_status()
                logger.info(f"✅ Telegram message sent to chat ID {chat_id} on attempt {attempt}.")
                return
            except Exception as e:
                logger.error(f"❌ Attempt {attempt}/{retries} failed for chat ID {chat_id}: {e}")
                if attempt < retries:
                    await asyncio.sleep(backoff * attempt)
    await asyncio.gather(*(send_to_chat(chat_id) for chat_id in CHAT_ID))

async def send_telegram_summary():
    global tickets_spotted, error_count
    if tickets_spotted == 0 and error_count == 0:  # Skip if no activity
        return
    now = datetime.now().strftime("%H:%M")
    message = (
        f"⏰ <b>Update</b> ({now}):\n"
        f"🎫 <b>Tickets Spotted</b>: {tickets_spotted}\n"
        f"⚠️ <b>Errors</b>: {error_count}\n"
    )
    logger.debug(f"Sending summary to {len(CHAT_ID)} chat IDs")
    await send_telegram_message(message)

def get_chrome_binary_path():
    env_path = os.getenv("CHROME_BIN")
    if env_path and os.path.exists(env_path):
        return env_path
    possible_paths = [
        "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        which("chromium"), which("google-chrome"), which("chrome")
    ]
    for path in possible_paths:
        if path and os.path.exists(path):
            return path
    return None

def get_chromedriver_path():
    possible_paths = ["/usr/bin/chromedriver", which("chromedriver")]
    for path in possible_paths:
        if path and os.path.exists(path):
            return path
    return None

def init_driver():
    global cookies_cache
    options = uc.ChromeOptions()
    chrome_binary = get_chrome_binary_path()
    if not chrome_binary:
        logger.error("Chromium browser not found. Ensure CHROME_BIN is set or Chromium is installed.")
        raise FileNotFoundError("Chromium browser executable not found")
    options.binary_location = chrome_binary
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36"
    ]
    options.add_argument(f"--user-agent={random.choice(user_agents)}")
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,720")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_experimental_option("prefs", {
        "profile.default_content_setting_values": {
            "images": 2,  # Disable images (reinforce)
            "javascript": 1  # Enable JavaScript (required for Twickets)
        },
        "network.throttling": {
            "download": 1000,  # 1 Mbps
            "upload": 1000,
            "latency": 40
        }
    })
    
    chromedriver_path = get_chromedriver_path()
    if chromedriver_path:
        service = Service(chromedriver_path, log_path="/app/logs/chromedriver.log")
        driver = uc.Chrome(service=service, options=options, browser_executable_path=chrome_binary)
    else:
        driver = uc.Chrome(options=options, browser_executable_path=chrome_binary)
    
    if cookies_cache:
        driver.get("https://www.twickets.live")
        for cookie in cookies_cache:
            driver.add_cookie(cookie)
        logger.info("Cookies loaded from cache.")
    else:
        try:
            driver.get("https://www.twickets.live")
            with open(COOKIE_FILE, "rb") as f:
                cookies_cache = pickle.load(f)
                for cookie in cookies_cache:
                    driver.add_cookie(cookie)
            logger.info("Cookies loaded from file.")
        except FileNotFoundError:
            logger.info("No cookie file found, starting fresh session.")
    logger.info("Chrome driver initialized.")
    return driver

def restart_driver(driver):
    global cookies_cache, rate_limit_count
    if driver:
        cookies_cache = driver.get_cookies()
        with open(COOKIE_FILE, "wb") as f:
            pickle.dump(cookies_cache, f)
        logger.info("Cookies saved to file.")
        driver.quit()
        logger.info("Existing driver closed.")
    logger.info("Restarting Chrome driver...")
    rate_limit_count = 0
    return init_driver()

def check_for_rate_limit(driver):
    global rate_limit_count
    try:
        page_source = driver.page_source.lower()
        block_terms = ["429 too many requests", "access denied", "blocked", "forbidden", "server error", "rate limit exceeded"]
        found_terms = [term for term in block_terms if term in page_source]
        if found_terms:
            logger.warning(f"Rate limit detected, pausing for {RATE_LIMIT_PAUSE_SECONDS}s. Terms: {found_terms}")
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            with open(f"/app/logs/page_source_{timestamp}.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source[:10000])  # Limit to 10KB
            logger.debug(f"Page source saved to page_source_{timestamp}.html")
            rate_limit_count += 1
            return True
        return False
    except Exception:
        return False

async def check_for_tickets(driver):
    global tickets_spotted, error_count, rate_limit_count, last_ticket_results, last_message_time
    
    try:
        logger.info(f"🌐 Loading event page: {EVENT_URL}")
        driver.delete_all_cookies()  # Clear cookies to reduce memory
        if cookies_cache:
            for cookie in cookies_cache:
                driver.add_cookie(cookie)
        try:
            driver.set_page_load_timeout(30)
            driver.get(EVENT_URL)
        except (TimeoutException, ReadTimeout, ReadTimeoutError, ConnectionError, WebDriverException) as e:
            logger.error(f"Timeout or connection error loading {EVENT_URL}: {e}")
            error_count += 1
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            with open(f"/app/logs/page_source_{timestamp}.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source[:10000] or "No page source available")
            logger.debug(f"Page source saved to page_source_{timestamp}.html")
            return

        logger.debug(f"Page title: {driver.title}, URL: {driver.current_url}")
        
        if check_for_rate_limit(driver):
            return

        try:
            wait = WebDriverWait(driver, 2)
            try:
                cookie_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll")))
                cookie_button.click()
                logger.info("Clicked cookies accept button.")
            except:
                logger.debug("Trying fallback '.cookie-accept'")
                cookie_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".cookie-accept")))
                cookie_button.click()
                logger.info("Clicked cookies accept button using CSS selector.")
            time.sleep(random.uniform(0.3, 0.8))  # Reduced sleep
        except Exception as e:
            logger.debug(f"No cookies popup found: {e}")

        event_name = "Unknown"
        location = "Unknown"
        event_date = "Unknown"
        try:
            wait = WebDriverWait(driver, 2)  # Reduced timeout
            event_name_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#eventName > span:nth-child(1)")))
            event_name = html.escape(event_name_element.text.strip() or "Unknown")
            logger.debug(f"Extracted event name: {event_name}")
        except Exception:
            pass
        try:
            venue_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#venueName > span:nth-child(2)")))
            city_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#locationShortName > span:nth-child(1)")))
            venue = html.escape(venue_element.text.strip() or "Unknown")
            city = html.escape(city_element.text.strip() or "Unknown")
            location = f"{venue}, {city}" if venue != "Unknown" and city != "Unknown" else venue or city or "Unknown"
            logger.debug(f"Extracted location: {location}")
        except Exception:
            pass
        try:
            date_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".inline-datetime")))
            event_date = html.escape(date_element.text.strip() or "Unknown")
            logger.debug(f"Extracted event date: {event_date}")
        except Exception:
            pass

        try:
            wait = WebDriverWait(driver, 2)
            no_tickets_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#no-listings-found > div:nth-child(1) > p:nth-child(1) > span:nth-child(1)")))
            if "sorry, we don't currently have any tickets for this event" in no_tickets_element.text.lower():
                logger.info("No tickets found")
                last_ticket_results = None
                return
        except Exception:
            pass

        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(random.uniform(1.0, 2.0))  # Reduced sleep
        except WebDriverException:
            pass

        TICKET_SELECTOR = ".buy-button"
        wait = WebDriverWait(driver, 2)
        ticket_items = []
        try:
            ticket_items = wait.until(EC.visibility_of_any_elements_located((By.CSS_SELECTOR, TICKET_SELECTOR)))
            logger.debug(f"Found {len(ticket_items)} ticket items")
        except Exception:
            logger.warning("No tickets found")
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            with open(f"/app/logs/page_source_{timestamp}.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source[:10000] or "No page source available")
            page_text = driver.page_source.lower() if driver.page_source else ""
            error_indicators = ["captcha", "blocked", "access denied", "forbidden"]
            found_indicators = [term for term in error_indicators if term in page_text]
            if found_indicators:
                logger.warning(f"Possible blocking detected: {found_indicators}")
            last_ticket_results = None
            return

        available_tickets = []
        for ticket in ticket_items:
            try:
                buy_button = ticket.find_elements(By.CSS_SELECTOR, "twickets-listing.width-max div.result-row-buy")
                if buy_button:
                    price = "Unknown"
                    ticket_type = "Unknown"
                    quantity = "Unknown"
                    try:
                        price_element = ticket.find_element(By.CSS_SELECTOR, "twickets-listing span strong:nth-child(2)")
                        price = html.escape(price_element.text.strip() or "Unknown")
                    except:
                        pass
                    try:
                        ticket_type_elements = ticket.find_elements(By.CSS_SELECTOR, "[id^='listingPriceTier']")
                        ticket_type = html.escape(ticket_type_elements[0].text.strip() or "Unknown") if ticket_type_elements else "Unknown"
                    except:
                        pass
                    try:
                        quantity_element = ticket.find_element(By.CSS_SELECTOR, "twickets-listing div:nth-child(2) span span")
                        quantity = html.escape(quantity_element.text.strip() or "Unknown")
                    except:
                        pass
                    available_tickets.append({"price": price, "quantity": quantity, "type": ticket_type})
            except Exception:
                pass

        if TEST_MODE and random.random() < 0.3:
            available_tickets.append({"price": f"£{random.randint(20, 100)}", "quantity": str(random.randint(1, 4)), "type": "General Admission"})

        if available_tickets:
            current_results = sorted([(t["price"], t["quantity"], t["type"]) for t in available_tickets])
            results_hash = str(current_results)
            count = len(available_tickets)
            should_send = False
            if last_ticket_results is None or results_hash != last_ticket_results:
                logger.info(f"🎫 New tickets: {count} ticket(s)")
                tickets_spotted += count
                should_send = True
            elif last_message_time is None or (datetime.now() - last_message_time) >= timedelta(hours=RESEND_INTERVAL_HOURS):
                logger.info(f"🎫 Resending tickets after {RESEND_INTERVAL_HOURS} hours: {count} ticket(s)")
                should_send = True
            else:
                logger.info(f"🎫 Identical tickets, skipping (last sent: {last_message_time})")

            if should_send:
                alert_msg = f"🚨 <b>Found {count} ticket(s) for {event_name}</b>\n"
                alert_msg += f"📍 <b>Location</b>: {location}\n"
                alert_msg += f"📅 <b>Date</b>: {event_date}\n"
                alert_msg += f"🔗 <a href=\"{html.escape(EVENT_URL)}\">Event Link</a>\n"
                alert_msg += "----------------------------------------\n"
                for i, ticket in enumerate(available_tickets, 1):
                    alert_msg += f"🎟️ <b>Ticket {i}</b>: <b>{ticket['type']}</b>\n"
                    alert_msg += f"   💷 <b>Price</b>: {ticket['price']}\n"
                    alert_msg += f"   🔢 <b>Quantity</b>: {ticket['quantity']}\n"
                    alert_msg += "----------------------------------------\n"
                try:
                    await send_telegram_message(alert_msg)
                    last_ticket_results = results_hash
                    last_message_time = datetime.now()
                except Exception as e:
                    logger.error(f"Failed to send Telegram message: {e}")
                    error_count += 1
        else:
            logger.info("No tickets with Buy button available.")
            last_ticket_results = None

    except Exception as e:
        error_count += 1
        logger.error(f"❌ Error during ticket check: {e}", exc_info=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        with open(f"/app/logs/page_source_{timestamp}.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source[:10000] or "No page source available")

def reset_stats_if_new_day():
    global current_day, tickets_spotted, error_count, rate_limit_count
    if date.today() != current_day:
        logger.info("🔄 New day - resetting stats.")
        current_day = date.today()
        tickets_spotted = 0
        error_count = 0
        rate_limit_count = 0

def get_adaptive_sleep_time(error_count, rate_limit_count):
    base_sleep = random.randint(SLEEP_MIN, SLEEP_MAX)
    if error_count > 5 or rate_limit_count > 2:
        return base_sleep + random.randint(10, 20)  # Reduced additional sleep
    return base_sleep

async def main_loop():
    global last_summary_time, error_count, rate_limit_count
    logger.info(f"🚀 Twickets bot started. TEST_MODE={TEST_MODE}, heartbeat every {HEARTBEAT_INTERVAL_MINUTES}m")
    driver = init_driver()
    iteration_count = 0

    try:
        while True:
            try:
                await check_for_tickets(driver)
                iteration_count += 1
                if iteration_count >= DRIVER_RESTART_INTERVAL or rate_limit_count >= RATE_LIMIT_RESTART_THRESHOLD:
                    driver = restart_driver(driver)
                    iteration_count = 0
            except Exception as e:
                logger.error(f"Error in check, restarting driver: {e}")
                driver = restart_driver(driver)
                error_count += 1

            reset_stats_if_new_day()
            if datetime.now() - last_summary_time >= timedelta(minutes=HEARTBEAT_INTERVAL_MINUTES):
                await send_telegram_summary()
                last_summary_time = datetime.now()

            sleep_time = get_adaptive_sleep_time(error_count, rate_limit_count)
            logger.info(f"Sleeping for {sleep_time}s before next check...")
            await asyncio.sleep(sleep_time)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    finally:
        driver.quit()
        logger.info("Driver closed, exiting.")

async def main():
    validate_env_vars()
    await main_loop()

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda signum, frame: asyncio.run(main_loop()).cancel())
    signal.signal(signal.SIGINT, lambda signum, frame: asyncio.run(main_loop()).cancel())
    asyncio.run(main())