import psycopg  # Swapped from pyodbc to support PostgreSQL cleanly
import requests
from bs4 import BeautifulSoup
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==============================
# CLOUD DATABASE CONNECTION
# ==============================
# Replace this placeholder with your exact External Connection String from Render!
DATABASE_URL = "postgresql://optcg_market_user:WaNZB22PS2F2ZRKliuFsuPbYZMLg9wYS@dpg-d84ssluq1p3s73a64ll0-a.ohio-postgres.render.com/optcg_market"

try:
    conn = psycopg.connect(DATABASE_URL)
    cursor = conn.cursor()
    print("Successfully connected to Cloud PostgreSQL Database!")
    
    # Pre-build our core infrastructure schemas right inside the cloud container
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Cards (
            UniqueCardId VARCHAR(100) PRIMARY KEY,
            CardNumber VARCHAR(20) NOT NULL,
            CardName VARCHAR(255) NOT NULL,
            Rarity VARCHAR(50) NOT NULL,
            SetId VARCHAR(20) NOT NULL,
            ImageUrl TEXT
        );
        CREATE TABLE IF NOT EXISTS PriceHistory (
            UniqueCardId VARCHAR(100) NOT NULL,
            MarketSet VARCHAR(20) NOT NULL,
            PriceJPY INT NOT NULL,
            RecordedDate DATE NOT NULL,
            PRIMARY KEY (UniqueCardId, MarketSet, RecordedDate)
        );
    """)
    conn.commit()
except Exception as e:
    print(f"Cloud Database initialization failed: {e}")
    exit()

# ==============================
# CONFIG
# ==============================
BASE_URL = "https://yuyu-tei.jp/sell/opc/s/"
HEADERS = {"User-Agent": "Mozilla/5.0"}

print("Detecting available sets dynamically...")

SETS = []

try:
    index_response = requests.get(BASE_URL, headers=HEADERS, timeout=15)
    index_soup = BeautifulSoup(index_response.text, "html.parser")

    all_links = index_soup.find_all("a", href=True)

    for link in all_links:
        href = link["href"]

        # We only want set URLs like /sell/opc/s/op15
        if "/sell/opc/s/" in href:
            set_code = href.split("/")[-1].lower()

            if set_code.startswith(("op", "st", "eb")):
                if set_code not in SETS:
                    SETS.append(set_code)

    SETS.sort()
    print(f"Detected sets: {SETS}")

except Exception as e:
    print(f"Set auto-detection failed: {e}")
    exit()

# ==============================
# MULTITHREADED SCRAPING
# ==============================

def process_set(set_code):
    local_cards = 0
    local_prices = 0

    try:
        # Each thread gets its own DB connection
        conn = psycopg.connect(DATABASE_URL)
        cursor = conn.cursor()

        print(f"\nProcessing {set_code.upper()}")

        url = f"{BASE_URL}{set_code}"
        response = requests.get(url, headers=HEADERS, timeout=15)

        if response.status_code != 200:
            print(f"Skipping {url} (Status {response.status_code})")
            return 0, 0

        soup = BeautifulSoup(response.text, "html.parser")
        card_boxes = soup.find_all("div", class_="card-product")

        for card in card_boxes:
            try:
                title_element = card.find("h4", class_="text-primary")
                if not title_element:
                    continue
                card_name = title_element.text.strip()

                number_element = card.find("span")
                if not number_element:
                    continue
                card_number = number_element.text.strip().upper()

                if "-" not in card_number:
                    continue

                price_element = card.find("strong")
                if not price_element:
                    continue
                price_text = price_element.text.strip()
                price_jpy = int("".join(filter(str.isdigit, price_text)))

                img_url = ""
                link_element = card.find("a", href=True)
                if link_element:
                    detail_url = link_element["href"]
                    parts = detail_url.strip("/").split("/")
                    if len(parts) >= 5:
                        set_code_img = parts[-2]
                        image_id = parts[-1]
                        img_url = f"https://card.yuyu-tei.jp/opc/front/{set_code_img}/{image_id}.jpg"

                rarity = "UNKNOWN"
                is_parallel = 0

                img_tag = card.find("img")
                if img_tag and img_tag.has_attr("alt"):
                    alt_text = img_tag["alt"].strip().upper()

                    if "パラレル" in alt_text:
                        is_parallel = 1

                    for explicit_rarity in ["P-SEC", "P-SR", "SP"]:
                        if explicit_rarity in alt_text:
                            rarity = explicit_rarity
                            break

                    if rarity == "UNKNOWN":
                        if "SEC" in alt_text:
                            rarity = "SEC"
                        elif "SR" in alt_text:
                            rarity = "SR"
                        elif "UC" in alt_text:
                            rarity = "UC"
                        elif "R" in alt_text:
                            rarity = "R"
                        elif "C" in alt_text:
                            rarity = "C"
                        elif "L" in alt_text:
                            rarity = "L"
                        elif "P" in alt_text:
                            rarity = "P"

                if is_parallel:
                    rarity += "_PARALLEL"

                origin_set = card_number.split("-")[0]
                market_set = set_code.upper()

                unique_card_id = f"{card_number}_{rarity}"

                cursor.execute("""
                    INSERT INTO Cards 
                    (UniqueCardId, CardNumber, CardName, Rarity, SetId, ImageUrl)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (UniqueCardId)
                    DO UPDATE SET
                        CardName = EXCLUDED.CardName,
                        ImageUrl = EXCLUDED.ImageUrl;
                """, (unique_card_id, card_number, card_name, rarity, origin_set, img_url))

                cursor.execute("""
                    INSERT INTO PriceHistory
                    (UniqueCardId, MarketSet, PriceJPY, RecordedDate)
                    VALUES (%s, %s, %s, CURRENT_DATE)
                    ON CONFLICT (UniqueCardId, MarketSet, RecordedDate)
                    DO UPDATE SET PriceJPY = EXCLUDED.PriceJPY;
                """, (unique_card_id, market_set, price_jpy))

                local_cards += 1
                local_prices += 1

            except Exception:
                continue

        conn.commit()
        cursor.close()
        conn.close()

        return local_cards, local_prices

    except Exception as e:
        print(f"Thread failed {set_code}: {e}")
        return 0, 0


print("\nStarting multithread scraping...\n")

cards_processed = 0
prices_logged = 0

with ThreadPoolExecutor(max_workers=5) as executor:
    futures = {executor.submit(process_set, set_code): set_code for set_code in SETS}

    for future in as_completed(futures):
        cards, prices = future.result()
        cards_processed += cards
        prices_logged += prices


print("\n=================================")
print("MULTITHREAD SCRAPING COMPLETE")
print(f"Total Cards Uploaded to Render: {cards_processed}")
print(f"Total Prices Logged to Render: {prices_logged}")
print("=================================")