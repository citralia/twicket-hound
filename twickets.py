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
import os

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc

load_dotenv(override=True)
chrome_bin = os.environ.get("CHROME_BIN", "/usr/bin/chromium")

# Configuration from environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID").split(",") if os.getenv("CHAT_ID") else []
EVENT_URL = os.getenv("EVENT_URL")
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"
HEARTBEAT_INTERVAL_MINUTES = int(os.getenv("HEARTBEAT_INTERVAL_MINUTES", 30))
SLEEP_MIN = int(os.getenv("SLEEP_MIN", 20))
SLEEP_MAX = int(os.getenv("SLEEP_MAX", 40))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 3))
DRIVER_RESTART_INTERVAL = int(os.getenv("DRIVER_RESTART_INTERVAL", 100))
RATE_LIMIT_RESTART_THRESHOLD = int(os.getenv("RATE_LIMIT_RESTART_THRESHOLD", 3))
RATE_LIMIT_PAUSE_SECONDS = int(os.getenv("RATE_LIMIT_PAUSE_SECONDS", 900))
last_ticket_results = None
last_message_time = None
RESEND_INTERVAL_HOURS = 4  # Resend identical results every 4 hours


# Logging setup with rotating files
from logging.handlers import RotatingFileHandler
log_handler = RotatingFileHandler("twickets.log", maxBytes=5*1024*1024, backupCount=3)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    handlers=[log_handler, logging.StreamHandler()],
)
logger = logging.getLogger()

# Stats and timing globals
tickets_spotted = 0
error_count = 0
rate_limit_count = 0
last_summary_time = datetime.now()
current_day = date.today()
COOKIE_FILE = "twickets_cookies.pkl"

def validate_env_vars():
    required_vars = ["TELEGRAM_BOT_TOKEN", "CHAT_ID"]
    missing = [var for var in required_vars if not os.getenv(var)]
    if missing:
        logger.error(f"Missing environment variables: {', '.join(missing)}")
        raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")
    logger.info(f"Loaded environment variables: EVENT_URL={EVENT_URL}, "
                f"TEST_MODE={TEST_MODE}, HEARTBEAT_INTERVAL_MINUTES={HEARTBEAT_INTERVAL_MINUTES}, "
                f"SLEEP_MIN={SLEEP_MIN}, SLEEP_MAX={SLEEP_MAX}, MAX_RETRIES={MAX_RETRIES}, "
                f"DRIVER_RESTART_INTERVAL={DRIVER_RESTART_INTERVAL}, "
                f"RATE_LIMIT_RESTART_THRESHOLD={RATE_LIMIT_RESTART_THRESHOLD}, "
                f"RATE_LIMIT_PAUSE_SECONDS={RATE_LIMIT_PAUSE_SECONDS}")
    logger.info("Environment variables validated successfully.")

def send_telegram_message(text, retries=MAX_RETRIES, backoff=5):
    for chat_id in CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id.strip(),
            "text": text,
            "parse_mode": "HTML",
        }
        for attempt in range(1, retries + 1):
            try:
                resp = requests.post(url, data=payload, timeout=10)
                resp.raise_for_status()
                logger.info(f"✅ Telegram message sent to chat ID {chat_id} on attempt {attempt}.")
                break  # Exit retry loop on success
            except Exception as e:
                logger.error(f"❌ Attempt {attempt}/{retries} failed to send Telegram message to chat ID {chat_id}: {e}")
                if attempt == retries:
                    logger.error(f"❌ Failed to send Telegram message to chat ID {chat_id} after {retries} retries.")
                time.sleep(backoff * attempt)
            else:
                break

def send_telegram_summary():
    global tickets_spotted, error_count
    now = datetime.now().strftime("%H:%M")
    message = (
        f"⏰ <b>Update</b> ({now}):\n"
        f"🎫 <b>Tickets Spotted</b>: {tickets_spotted}\n"
        f"⚠️ <b>Errors</b>: {error_count}\n"
    )
    logger.debug(f"Sending summary message to {len(CHAT_ID)} chat IDs: {CHAT_ID}")
    send_telegram_message(message)

def init_driver():
    options = Options()
    options.binary_location = chrome_bin
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
    ]
    options.add_argument(f"--user-agent={random.choice(user_agents)}")
    # Comment out headless mode to observe browser
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(30)
    try:
        driver.get("https://www.twickets.live")
        with open(COOKIE_FILE, "rb") as f:
            cookies = pickle.load(f)
            for cookie in cookies:
                driver.add_cookie(cookie)
        logger.info("Cookies loaded from file.")
    except FileNotFoundError:
        logger.info("No cookie file found, starting fresh session.")
    logger.info("Chrome driver initialized.")
    return driver

def restart_driver(driver):
    global rate_limit_count
    if driver:
        cookies = driver.get_cookies()
        with open(COOKIE_FILE, "wb") as f:
            pickle.dump(cookies, f)
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
        found_terms = []
        for term in block_terms:
            if term in page_source:
                found_terms.append(term)
                match = re.search(term, page_source, re.IGNORECASE)
                if match:
                    start = max(0, match.start() - 100)
                    end = min(len(page_source), match.end() + 100)
                    context = page_source[start:end].replace('\n', ' ')
                    logger.debug(f"Context for '{term}': ...{context}...")
        if found_terms:
            logger.warning(f"Rate limit or error page detected, pausing for {RATE_LIMIT_PAUSE_SECONDS} seconds. Found blocking terms: {found_terms}")
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            with open(f"page_source_{timestamp}.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            logger.debug(f"Page source saved to page_source_{timestamp}.html")
            rate_limit_count += 1
            time.sleep(RATE_LIMIT_PAUSE_SECONDS)
            return True
        return False
    except Exception:
        return False

def check_for_tickets(driver):
    global tickets_spotted, error_count, rate_limit_count, last_ticket_results, last_message_time
    try:
        logger.info(f"🌐 Loading event page: {EVENT_URL}")
        driver.get(EVENT_URL)
        logger.debug(f"Page title: {driver.title}, URL: {driver.current_url}, Chrome version: {driver.capabilities['browserVersion']}, Headless: {driver.capabilities.get('chrome', {}).get('headless', False)}")
        if check_for_rate_limit(driver):
            return

        # Handle cookies popup
        try:
            wait = WebDriverWait(driver, 2)
            try:
                cookie_button = wait.until(EC.element_to_be_clickable((By.XPATH, "/html/body/div[1]/div/div[4]/div[1]/div/div[2]/button[1]")))
                cookie_button.click()
                logger.info("Clicked cookies accept button using XPath.")
            except:
                logger.debug("XPath for cookies button failed, trying fallback CSS selector '.cookie-accept'")
                cookie_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".cookie-accept")))
                cookie_button.click()
                logger.info("Clicked cookies accept button using CSS selector.")
            time.sleep(random.uniform(0.5, 1.5))
        except Exception as e:
            logger.debug(f"No cookies popup found or failed to click: {e}")

        # Extract event details
        event_name = "Unknown"
        location = "Unknown"
        event_date = "Unknown"
        try:
            wait = WebDriverWait(driver, 3)
            event_name_element = wait.until(EC.presence_of_element_located((By.XPATH, "/html/body/div/div[1]/div[2]/div[1]/div[1]/div/div[2]/div/div[1]/h1/span[1]")))
            event_name = html.escape(event_name_element.text.strip() or "Unknown")
            logger.debug(f"Extracted event name: {event_name}")
        except Exception as e:
            logger.debug(f"Failed to extract event name: {e}")
        try:
            venue_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#venueName > span:nth-child(2)")))
            city_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#locationShortName > span:nth-child(1)")))
            venue = html.escape(venue_element.text.strip() or "Unknown")
            city = html.escape(city_element.text.strip() or "Unknown")
            location = f"{venue}, {city}" if venue != "Unknown" and city != "Unknown" else venue or city or "Unknown"
            logger.debug(f"Extracted location: {location}")
        except Exception as e:
            logger.debug(f"Failed to extract location: {e}")
        try:
            date_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".inline-datetime")))
            event_date = html.escape(date_element.text.strip() or "Unknown")
            logger.debug(f"Extracted event date: {event_date}")
        except Exception as e:
            logger.debug(f"Failed to extract event date: {e}")

        # Check for "no tickets" message
        try:
            wait = WebDriverWait(driver, 2)
            no_tickets_element = wait.until(EC.presence_of_element_located((By.XPATH, "/html/body/div[2]/div[1]/div[2]/div[5]/div/p/span")))
            no_tickets_text = no_tickets_element.text.lower()
            if "sorry, we don't currently have any tickets for this event" in no_tickets_text:
                logger.info(f"No tickets found")
                return
        except Exception as e:
            logger.debug(f"No 'no tickets' message found or failed to check: {e}")

        # Ensure full page load
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(random.uniform(3.0, 6.0))  # Increased delay for dynamic content

        wait = WebDriverWait(driver, 6) 
        ticket_items = []
        selector_attempts = [
            ("ul#list.tickets > li", "primary selector 'ul#list.tickets > li'"),
            ("[id*='listing'] li", "fallback selector '[id*=\"listing\"] li'"),
            ("twickets-listing", "final fallback 'twickets-listing'"),
            ("div[class*='listing']", "broad fallback 'div[class*=\"listing\"]'")
        ]
        for selector, desc in selector_attempts:
            try:
                wait.until(EC.visibility_of_any_elements_located((By.CSS_SELECTOR, selector)))
                ticket_items = driver.find_elements(By.CSS_SELECTOR, selector)
                logger.debug(f"Found {len(ticket_items)} ticket items with {desc}")
                break
            except Exception as e:
                logger.warning(f"Selector '{selector}' failed: {e}")

        if not ticket_items:
            logger.error(f"All ticket selectors failed")
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            with open(f"page_source_{timestamp}.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            logger.debug(f"Page source saved to page_source_{timestamp}.html")
            page_text = driver.page_source.lower()
            error_indicators = ["captcha", "blocked", "access denied", "error", "forbidden", "429"]
            found_indicators = [term for term in error_indicators if term in page_text]
            if found_indicators:
                logger.warning(f"Possible blocking detected: {found_indicators}")
            return

        logger.debug(f"Found {len(ticket_items)} ticket items")

        available_tickets = []

        for ticket in ticket_items:
            try:
                buy_button = ticket.find_elements(By.CSS_SELECTOR, "twickets-listing.width-max div.result-row-buy")
                if buy_button:
                    try:
                        price_element = ticket.find_element(By.CSS_SELECTOR, "twickets-listing span strong:nth-child(2)")
                        price = html.escape(price_element.text.strip() or "Unknown")
                        logger.debug(f"Extracted price: {price}")
                    except:
                        price = "Unknown"
                        logger.debug("Failed to extract price")
                    try:
                        ticket_type_elements = ticket.find_elements(By.CSS_SELECTOR, "[id^='listingPriceTier']")
                        ticket_type = html.escape(ticket_type_elements[0].text.strip() or "Unknown") if ticket_type_elements else "Unknown"
                        logger.debug(f"Extracted ticket type: {ticket_type}")
                    except:
                        ticket_type = "Unknown"
                        logger.debug("Failed to extract ticket type")
                    try:
                        quantity_element = ticket.find_element(By.CSS_SELECTOR, "twickets-listing div:nth-child(2) span span")
                        quantity = html.escape(quantity_element.text.strip() or "Unknown")
                        logger.debug(f"Extracted quantity: {quantity}")
                    except:
                        quantity = "Unknown"
                        logger.debug("Failed to extract quantity")
                    available_tickets.append({"price": price, "quantity": quantity, "type": ticket_type})
                else:
                    logger.debug("No Buy button found for this ticket, skipping.")
            except Exception as e:
                logger.warning(f"Failed to parse ticket details: {e}")
                available_tickets.append({"price": "Unknown", "quantity": "Unknown", "type": "Unknown"})

        if TEST_MODE and random.random() < 0.3:
            logger.debug(f"Simulating ticket find in TEST_MODE")
            available_tickets.append({"price": f"£{random.randint(20, 100)}", "quantity": str(random.randint(1, 4)), "type": "General Admission"})

        if available_tickets:
            # Create a comparable representation of ticket results
            current_results = sorted([(t["price"], t["quantity"], t["type"]) for t in available_tickets])
            results_hash = str(current_results)
            count = len(available_tickets)

            # Check if results are identical to last sent
            should_send = False
            if last_ticket_results is None or results_hash != last_ticket_results:
                logger.info(f"🎫 New or changed tickets found: {count} ticket(s) available!")
                tickets_spotted += count  # Increment only for new/changed tickets
                should_send = True
            elif last_message_time is None or (datetime.now() - last_message_time) >= timedelta(hours=RESEND_INTERVAL_HOURS):
                logger.info(f"🎫 Resending identical tickets after {RESEND_INTERVAL_HOURS} hours: {count} ticket(s) available!")
                should_send = True
            else:
                logger.info(f"🎫 Identical tickets found, skipping message (last sent: {last_message_time})")

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
                logger.debug(f"Sending ticket alert to {len(CHAT_ID)} chat IDs: {CHAT_ID}")
                send_telegram_message(alert_msg)
                last_ticket_results = results_hash
                last_message_time = datetime.now()
        else:
            logger.info(f"No tickets with Buy button available right now.")
            last_ticket_results = None  # Reset if no tickets found

    except Exception as e:
        error_count += 1
        logger.error(f"❌ Error during ticket check: {e}", exc_info=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        with open(f"page_source_{timestamp}.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        logger.debug(f"Page source saved to page_source_{timestamp}.html")

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
        return base_sleep + random.randint(10, 30)
    return base_sleep

def handle_shutdown(signum, frame):
    logger.info("Received shutdown signal, exiting gracefully...")
    raise KeyboardInterrupt

def main_loop():
    global last_summary_time, error_count, rate_limit_count
    logger.info(
        f"🚀 Twickets bot started. TEST_MODE={TEST_MODE}, heartbeat every {HEARTBEAT_INTERVAL_MINUTES} minutes."
    )
    driver = init_driver()
    iteration_count = 0

    try:
        while True:
            try:
                check_for_tickets(driver)
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
                send_telegram_summary()
                last_summary_time = datetime.now()

            sleep_time = get_adaptive_sleep_time(error_count, rate_limit_count)
            logger.info(f"Sleeping for {sleep_time} seconds before next check...")
            time.sleep(sleep_time)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    finally:
        driver.quit()
        logger.info("Driver closed, exiting.")

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    validate_env_vars()
    main_loop()