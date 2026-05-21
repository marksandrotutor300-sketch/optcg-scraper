import os
import psycopg  # Swapped from pyodbc to support PostgreSQL cleanly
import requests
from bs4 import BeautifulSoup
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==============================
# CLOUD DATABASE CONNECTION
# ==============================
DATABASE_URL = os.getenv("DATABASE_URL")

def create_connection():
    # SAFETY CATCH FOR RENDER BUILD PHASE:
    if not DATABASE_URL:
        print("⚠️ DATABASE_URL is missing. (This is normal if Render is running a Build Check)")
        return None
    return psycopg.connect(DATABASE_URL)

def initialize_database():
    try:
        print("Initializing cloud database infrastructure...")
        conn = create_connection()
        
        if conn is None:
            print("Plugged build-phase safety bypass. Database initialization skipped during build.")
            return

        cursor = conn.cursor()
        
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
        cursor.close()
        conn.close()
        print("Successfully connected and verified Cloud PostgreSQL schemas!")
    except Exception as e:
        print(f"Cloud Database initialization failed: {e}")
        if DATABASE_URL:
            exit(1)

# ==============================
# CONFIG
# ==============================
BASE_URL = "https://yuyu-tei.jp/sell/opc/s/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

# ==============================
# MULTITHREADED SCRAPING
# ==============================
def process_set(set_code):
    local_cards = 0
    local_prices = 0

    try:
        # Each thread requests its own unique DB connection
        conn = create_connection()
        cursor = conn.cursor()

        print(f"Processing set: {set_code.upper()}")
        time.sleep(0.5)  # Gentle pacing cushion to prevent rate limiting

        url = f"{BASE_URL}{set_code}"
        response = requests.get(url, headers=HEADERS, timeout=15)

        if response.status_code != 200:
            print(f"Skipping {url} (Status {response.status_code})")
            return 0, 0

        soup = BeautifulSoup(response.text, "html.parser")
        
        # Yuyu-tei groups cards under header blocks like <h3 class="title">SEC Card List</h3>
        # We will loop through the category sections directly to get correct base rarities!
        sections = soup.find_all(["h3", "h4"], class_="title")
        if not sections:
            # Fallback if text layout differs
            sections = soup.find_all("div", class_="sub-box")

        # Let's search the full page with high precision
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
                raw_number = number_element.text.strip().upper().replace(" ", "")

                if "-" not in raw_number:
                    if raw_number.startswith(("OP", "ST", "EB")) and len(raw_number) >= 7:
                        card_number = f"{raw_number[:4]}-{raw_number[4:]}"
                    else:
                        continue 
                else:
                    card_number = raw_number

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

                # =========================================================
                # 🛠️ FIXED HIGH-PRECISION VARIANT PARSER
                # =========================================================
                rarity = "SEC" # Default fallback for secret rare block drops
                chase_variant = ""
                
                # Check the closest group section header above this card box to find the exact list it belongs to
                parent_section = card.find_parent("div", class_="card-list-box")
                section_text = ""
                if parent_section:
                    header = parent_section.find(["h3", "h4", "h5"])
                    if header:
                        section_text = header.text.strip().upper()

                # 1. Evaluate True Base List Rarity 
                if "P-SEC" in section_text:
                    rarity = "P-SEC"
                elif "SP" in section_text or "SP CARD" in section_text:
                    rarity = "SP"
                elif "SEC" in section_text:
                    rarity = "SEC"
                elif "P-SR" in section_text:
                    rarity = "P-SR"
                elif "SR" in section_text:
                    rarity = "SR"
                elif "P-R" in section_text:
                    rarity = "P-R"
                elif "R" in section_text:
                    rarity = "R"
                elif "UC" in section_text:
                    rarity = "UC"
                elif "C" in section_text:
                    rarity = "C"
                elif "L" in section_text:
                    rarity = "L"

                # 2. Check Alt-Text for specific chase styles (Manga / Wanted Poster / Red)
                img_tag = card.find("img")
                if img_tag and img_tag.has_attr("alt"):
                    alt_text = img_tag["alt"].strip().upper()
                    
                    # Detect Wanted Poster style frames (手配書 = Wanted Poster)
                    if "手配書" in alt_text or "WANTED" in alt_text:
                        chase_variant = "_WANTED"
                    
                    # Detect Red Super Parallel (レッドスーパーパラレル)
                    elif "レッド" in alt_text or "RED" in alt_text:
                        chase_variant = "_RED_MANGA"
                        rarity = "P-SEC"
                        
                    # Detect Standard Super Parallel Manga (スーパーパラレル)
                    elif "スーパーパラレル" in alt_text or "SUPER" in alt_text or "コミック" in alt_text:
                        chase_variant = "_MANGA"
                        rarity = "P-SEC"
                        
                    # Detect regular Alternate Arts if inside a Parallel block but not manga
                    elif "パラレル" in alt_text and not chase_variant and rarity == "SEC":
                        rarity = "P-SEC"

                # 3. Create completely isolated, non-colliding primary keys!
                if chase_variant:
                    unique_card_id = f"{card_number}_{rarity}{chase_variant}"
                else:
                    unique_card_id = f"{card_number}_{rarity}"

                origin_set = card_number.split("-")[0].strip()
                market_set = set_code.upper().strip()

                # Write to Database
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

            except Exception as e:
                continue
            
            try:
                title_element = card.find("h4", class_="text-primary")
                if not title_element:
                    continue
                card_name = title_element.text.strip()

                number_element = card.find("span")
                if not number_element:
                    continue
                # Clean text and remove spaces completely
                raw_number = number_element.text.strip().upper().replace(" ", "")

                # FIX: Auto-inject hyphens for Starter Decks and Extra Boosters if missing
                if "-" not in raw_number:
                    if raw_number.startswith(("OP", "ST", "EB")) and len(raw_number) >= 7:
                        # Converts OP14001 to OP14-001 or ST01001 to ST01-001
                        card_number = f"{raw_number[:4]}-{raw_number[4:]}"
                    else:
                        continue # Safely skip actual invalid anomalies
                else:
                    card_number = raw_number

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
                chase_variant = ""

                img_tag = card.find("img")
                if img_tag and img_tag.has_attr("alt"):
                    alt_text = img_tag["alt"].strip().upper()

                    # 1. Detect Parallel Treatment
                    if "パラレル" in alt_text:
                        is_parallel = 1

                    # 2. Extract Base Rarity
                    if "P-SEC" in alt_text:
                        rarity = "P-SEC"
                    elif "P-SR" in alt_text:
                        rarity = "P-SR"
                    elif "SP" in alt_text:
                        rarity = "SP"
                    elif "SEC" in alt_text:
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

                    # 3. ADVANCED JPN TEXT MATCHING FOR MANGA & SPECIAL VARIANTS
                    if "コミック" in alt_text or "原作" in alt_text:
                        chase_variant = "_MANGA"
                    
                    if "赤" in alt_text or "RED" in alt_text:
                        chase_variant = "_RED_MANGA"
                    elif "周年" in alt_text or "ANNIVERSARY" in alt_text:
                        chase_variant = "_ANNIV"

                # 4. Finalize Identity Strings cleanly
                if is_parallel and "P-" not in rarity and "SP" not in rarity:
                    rarity += "_PARALLEL"

                if chase_variant:
                    unique_card_id = f"{card_number}_{rarity}{chase_variant}"
                else:
                    unique_card_id = f"{card_number}_{rarity}"

                # FIX: Explicitly define origin_set and market_set before SQL executions
                origin_set = card_number.split("-")[0].strip()
                market_set = set_code.upper().strip()

                # 1. Store the unique card details
                cursor.execute("""
                    INSERT INTO Cards 
                    (UniqueCardId, CardNumber, CardName, Rarity, SetId, ImageUrl)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (UniqueCardId)
                    DO UPDATE SET
                        CardName = EXCLUDED.CardName,
                        ImageUrl = EXCLUDED.ImageUrl;
                """, (unique_card_id, card_number, card_name, rarity, origin_set, img_url))

                # 2. Store the price under the current Market Set list page
                cursor.execute("""
                    INSERT INTO PriceHistory
                    (UniqueCardId, MarketSet, PriceJPY, RecordedDate)
                    VALUES (%s, %s, %s, CURRENT_DATE)
                    ON CONFLICT (UniqueCardId, MarketSet, RecordedDate)
                    DO UPDATE SET PriceJPY = EXCLUDED.PriceJPY;
                """, (unique_card_id, market_set, price_jpy))

                local_cards += 1
                local_prices += 1

            except Exception as e:
                # Useful to keep an internal log stream during development anomalies
                # print(f"Card error: {e}")
                continue

        conn.commit()
        cursor.close()
        conn.close()

        return local_cards, local_prices

    except Exception as e:
        print(f"Thread failed for set {set_code}: {e}")
        return 0, 0

# ==============================
# MAIN RUNNER EXECUTION 
# ==============================
if __name__ == "__main__":
    # 1. Initialize Tables Safely
    initialize_database()

    print("Pre-building safety target list for all One Piece TCG sets...")
    baseline_sets = []
    
    # 1. Main Booster Sets (OP-01 through OP-15)
    baseline_sets += [f"op{str(i).zfill(2)}" for i in range(1, 16)] 
    
    # 2. Extra Boosters (EB-01 through EB-04)
    baseline_sets += [f"eb{str(i).zfill(2)}" for i in range(1, 5)]   
    
    # 3. Starter Decks (ST-01 through ST-30)
    baseline_sets += [f"st{str(i).zfill(2)}" for i in range(1, 31)]  

    # ==============================
    # DYNAMIC AUTO-DETECTION COUPLING
    # ==============================
    SETS = []
    try:
        index_response = requests.get(BASE_URL, headers=HEADERS, timeout=15)
        if index_response.status_code == 200:
            index_soup = BeautifulSoup(index_response.text, "html.parser")
            all_links = index_soup.find_all("a", href=True)

            for link in all_links:
                href = link["href"]
                if "/sell/opc/s/" in href:
                    set_code = href.split("/")[-1].lower()
                    if set_code.startswith(("op", "st", "eb")):
                        if set_code not in SETS:
                            SETS.append(set_code)

        for b_set in baseline_sets:
            if b_set not in SETS:
                SETS.append(b_set)

        SETS.sort()
        print(f"🎯 Execution Map Verified! Total target sets queued: {len(SETS)}")
        print(f"📋 Full Target List: {SETS}")

    except Exception as e:
        print(f"⚠️ Dynamic link engine paused: {e}. Defaulting straight to maximum safe manifests.")
        SETS = baseline_sets
        SETS.sort()

    # ==============================
    # MULTITHREAD EXECUTION RUNNER
    # ==============================
    print("\nStarting multithread scraping across all One Piece generations...\n")
    cards_processed = 0
    prices_logged = 0

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(process_set, set_code): set_code for set_code in SETS}
        for future in as_completed(futures):
            cards, prices = future.result()
            cards_processed += cards
            prices_logged += prices

    print("\n=================================")
    print("🚀 MULTITHREAD SCRAPING COMPLETE")
    print(f"Total Cards Live Updated: {cards_processed}")
    print(f"Total Price Milestones Tracked: {prices_logged}")
    print("=================================\n")