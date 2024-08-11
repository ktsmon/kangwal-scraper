from quart import Quart, jsonify, request, current_app
import asyncio
from aiohttp import ClientSession
from bs4 import BeautifulSoup
import re
from concurrent.futures import ThreadPoolExecutor
import undetected_chromedriver as uc
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from functools import partial
import os
from functools import wraps

app = Quart(__name__)
API_KEY = os.environ.get('API_KEY')
WORKERS = int(os.environ.get('WORKERS'))
if not API_KEY:
    raise ValueError("No API key set for the application. Please set the API_KEY environment variable.")

app.config['API_KEY'] = API_KEY

# Create a thread pool for running Selenium tasks
thread_pool = ThreadPoolExecutor(max_workers=WORKERS)  # Adjust based on your server's capacity

# Create a connection pool for browser instances
browser_pool = []
MAX_BROWSERS = WORKERS  # Adjust based on your server's capacity


def require_api_key(view_function):
    @wraps(view_function)
    async def decorated_function(*args, **kwargs):
        # Ensure you're using the request context
        api_key = request.args.get('api_key')

        if api_key and api_key == current_app.config.get('API_KEY'):
            return await view_function(*args, **kwargs)
        else:
            return jsonify({"error": "Invalid or missing API key"}), 403

    return decorated_function


def create_browser():
    options = Options()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    return uc.Chrome(options=options)


for _ in range(MAX_BROWSERS):
    browser_pool.append(create_browser())


async def get_tour_href(tour_id):
    browser = None
    try:
        browser = browser_pool.pop()
        search_url = "https://www.go365travel.com/"
        browser.get(search_url)

        search_bar = WebDriverWait(browser, 10).until(
            EC.presence_of_element_located((By.XPATH, "//input[@placeholder='จะไปเที่ยวที่ไหน? หาสะดวก รวดเร็ว...']"))
        )
        search_bar.send_keys(tour_id)
        search_button = browser.find_element(By.XPATH, "//button[@class='btn btn-info btn-lg']")
        search_button.click()

        WebDriverWait(browser, 10).until(
            EC.presence_of_element_located((By.CLASS_NAME, "tour-box-main"))
        )
        soup = BeautifulSoup(browser.page_source, 'html.parser')
        tour_elem = soup.find('div', class_='tour-box-main')
        return tour_elem.find('a').get('href')
    finally:
        if browser:
            browser_pool.append(browser)


async def fetch_tour_data(session, tour_url):
    async with session.get(tour_url) as response:
        return await response.text()


async def scrape_tour_data(tour_id):
    tour_href = await asyncio.get_event_loop().run_in_executor(
        thread_pool, partial(asyncio.run, get_tour_href(tour_id))
    )

    tour_url = f"https://www.go365travel.com{tour_href}"

    async with ClientSession() as session:
        html_content = await fetch_tour_data(session, tour_url)

    soup = BeautifulSoup(html_content, 'html.parser')
    short_desc = soup.find('div', class_='short-description')

    tour_data = {
        "รหัสโปรแกรม": "N/A",
        "ชื่อทัวร์": "N/A",
        "เมือง": "N/A",
        "ระยะเวลา": {"วัน": "N/A", "คืน": "N/A"},
        "ราคานั้นเริ่มต้น": "N/A",
        "ราคาพิเศษ": "N/A",
        "เที่ยว": "N/A",
        "ช้อปปิ้ง": "-",
        "พิเศษ": "-",
        "โรงแรม": "-",
        "สายการบิน": "N/A",
        "กำหนดการ": [],
        "tour_url": tour_url
    }

    if short_desc:
        tour_name_elem = soup.find('h1', class_='font-topic')
        if tour_name_elem:
            tour_data["ชื่อทัวร์"] = tour_name_elem.text.strip()

        tour_code_elem = short_desc.find('input', id='linkCode')
        if tour_code_elem:
            tour_data["รหัสโปรแกรม"] = tour_code_elem.get('value', 'N/A')

        city_elem = short_desc.find('span', string=lambda x: x and 'เที่ยวเมือง :' in x)
        if city_elem:
            tour_data["เมือง"] = city_elem.next_sibling.next_element.text.strip()

        airline_elem = soup.find('span', string=lambda x: x and 'สายการบิน :' in x)
        if airline_elem:
            tour_data["สายการบิน"] = airline_elem.next_sibling.text.strip()

    duration_pattern = re.compile(r'^\s*\d+\s*วัน\s*\d+\s*คืน\s*$')
    duration_elem = soup.find('span', string=duration_pattern)
    if duration_elem:
        duration_text = duration_elem.text.strip()
        parts = duration_text.split()
        if len(parts) >= 4:
            tour_data["ระยะเวลา"]["วัน"] = parts[0]
            tour_data["ระยะเวลา"]["คืน"] = parts[2]

    highlight_elem = soup.find('span', class_='descript_hilight')
    if highlight_elem:
        tour_data["เที่ยว"] = highlight_elem.text.strip()

    timeline_boxes = soup.find_all('div', class_='timeline__box')
    for day_num, box in enumerate(timeline_boxes, start=1):
        day_description_elem = box.find('p', class_='dayTopic')
        day_description = day_description_elem.text.strip() if day_description_elem else "No description available"
        tour_data["กำหนดการ"].append({f"วันที่ {day_num}": day_description})

    prices = soup.find('div', class_='price')
    if prices:
        starting_price_elem = prices.find('span', style=lambda x: 'font-size:26px' in x)
        if starting_price_elem:
            tour_data["ราคานั้นเริ่มต้น"] = starting_price_elem.text.strip()

        special_price_elem = prices.find('span', style="font-size:38px !important;")
        if special_price_elem:
            tour_data["ราคาพิเศษ"] = special_price_elem.text.strip()

    return tour_data


@app.route('/tour_data', methods=['GET'])
@require_api_key
async def get_tour_data():
    tour_id = request.args.get('tour_id')
    if not tour_id:
        return jsonify({"error": "Missing tour_id parameter"}), 400

    try:
        tour_data = await scrape_tour_data(tour_id)
        return jsonify(tour_data)
    except Exception as e:
        return jsonify({"error": f"Failed to retrieve tour data: {str(e)}"}), 500