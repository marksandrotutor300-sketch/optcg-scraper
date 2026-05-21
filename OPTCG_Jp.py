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
            print(f"Skiipping {url} (Status {response.status_code})")
            return 0, 0

        soup = BeautifulSoup(response.text, "html.parser")
        card_boxes = soup.find_all("div", class_="card-product")

        for card in card_boxes:
            try:
                # 1. Extract Card Name (Always inside h4 text-primary)
                title_element = card.find("h4", class_="text-primary")
                if not title_element:
                    continue
                card_name = title_element.text.strip()

                # 2. Extract Card Number (Look for the centered border-dark block text)
                number_element = card.find("span", class_="border-dark")
                if not number_element:
                    # Fallback to general span search if layout shifts
                    number_element = card.find("span")
                    if not number_element or "-" not in number_element.text:
                        continue
                
                raw_number = number_element.text.strip().upper().replace(" ", "")
                
                # Normalize format to always use hyphens (e.g., EB01-001)
                if "-" not in raw_number:
                    if raw_number.startswith(("OP", "ST", "EB")) and len(raw_number) >= 7:
                        card_number = f"{raw_number[:4]}-{raw_number[4:]}"
                    else:
                        continue 
                else:
                    card_number = raw_number

                # 3. Extract Price (Find the strong tag safely anywhere inside the card block)
                price_element = card.find("strong")
                if not price_element:
                    continue
                price_text = price_element.text.strip()
                price_jpy = int("".join(filter(str.isdigit, price_text)))

                # 4. Extract Image URL Layout
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
                # 🛠️ EXPLICIT FIELD DATA LOGIC 
                # =========================================================
                rarity = "UNKNOWN"
                chase_variant = ""

                # Target the exact badge wrapper text (e.g., P-L, L, SEC, P-SEC)
                rarity_span = card.find("span", class_="text-white")
                if rarity_span:
                    rarity = rarity_span.text.strip().upper()
                
                # Check Card Name and Image Alt text for Manga/Special Chase tags
                img_tag = card.find("img")
                alt_text = img_tag["alt"].strip().upper() if (img_tag and img_tag.has_attr("alt")) else ""
                combined_text = f"{card_name.upper()} {alt_text}"

                # Isolate High-Value Super Parallel Chase variations safely
                if "手配書" in combined_text or "WANTED" in combined_text:
                    chase_variant = "_WANTED"
                    rarity = "SP"
                elif "レッド" in combined_text or "RED" in combined_text:
                    chase_variant = "_RED_MANGA"
                    rarity = "P-SEC"
                elif "スーパーパラレル" in combined_text or "SUPER" in combined_text or "コミック" in combined_text or "原作" in combined_text:
                    chase_variant = "_MANGA"
                    rarity = "P-SEC"

                # 5. Build Bulletproof Non-Colliding ID Keys
                if chase_variant:
                    unique_card_id = f"{card_number}_{rarity}{chase_variant}"
                else:
                    unique_card_id = f"{card_number}_{rarity}"

                # If it's a generic secondary Alternate Art sharing a common parallel rarity key
                if img_url:
                    img_id = img_url.split("/")[-1].split(".")[0]
                    if "P-" in rarity and not chase_variant and not img_id.endswith("101") and not img_id.endswith("100"):
                        unique_card_id = f"{card_number}_{rarity}_ALT"

                origin_set = card_number.split("-")[0].strip()
                market_set = set_code.upper().strip()

                # 6. Push Verified Row Elements to Database
                cursor.execute("""
                    INSERT INTO Cards 
                    (UniqueCardId, CardNumber, CardName, Rarity, SetId, ImageUrl)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (UniqueCardId)
                    DO UPDATE SET
                        CardName = EXCLUDED.CardName,
                        ImageUrl = EXCLUDED.ImageUrl,
                        Rarity = EXCLUDED.Rarity;
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
                raw_number = number_element.text.strip().upper().replace(" ", "")

                # Uniform hyphen processing
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
                # 🛠️ ULTRA-PRECISE VARIANT & RARITY EXTRACTOR
                # =========================================================
                rarity = "C"  # Baseline fallback
                chase_variant = ""

                # 1. Target the exact HTML rarity badge element used by YuYu-Tei
                rarity_element = card.find("em", class_="rarity")
                if rarity_element:
                    rarity = rarity_element.text.strip().upper()
                else:
                    # Alternative check if the wrapper contains rarity class selectors
                    card_classes = card.get("class", [])
                    for cls in card_classes:
                        if cls.startswith("rarity-"):
                            rarity = cls.split("-")[-1].upper()

                # 2. Check Image Alt Tags for Parallel Types and Special Chases
                img_tag = card.find("img")
                if img_tag and img_tag.has_attr("alt"):
                    alt_text = img_tag["alt"].strip().upper()

                    # Detect Wanted Poster style frames (手配書 / WANTED)
                    if "手配書" in alt_text or "WANTED" in alt_text:
                        chase_variant = "_WANTED"
                        rarity = "SP"

                    # Detect Red Super Parallel (レッドスーパーパラレル / 赤)
                    elif "レッド" in alt_text or "RED" in alt_text:
                        chase_variant = "_RED_MANGA"
                        rarity = "P-SEC"

                    # Detect Standard Super Parallel Manga (スーパーパラレル / コミック / 原作)
                    elif "スーパーパラレル" in alt_text or "SUPER" in alt_text or "コミック" in alt_text or "原作" in alt_text:
                        chase_variant = "_MANGA"
                        rarity = "P-SEC"

                    # Standardize parallel indicator notation
                    elif "パラレル" in alt_text and "P-" not in rarity and "SP" not in rarity:
                        if rarity in ["SEC", "SR", "R", "UC", "C", "L"]:
                            rarity = f"P-{rarity}"
                        else:
                            rarity += "_PARALLEL"

                # 3. Handle specific Alt Art separations that share the generic P-SEC tag
                if chase_variant:
                    unique_card_id = f"{card_number}_{rarity}{chase_variant}"
                else:
                    unique_card_id = f"{card_number}_{rarity}"

                if img_url:
                    img_id = img_url.split("/")[-1].split(".")[0]
                    # If it's an alternate art sharing the base P-SEC code without being manga, suffix it
                    if rarity == "P-SEC" and not chase_variant and not img_id.endswith("101"):
                        unique_card_id = f"{card_number}_{rarity}_ALT"

                origin_set = card_number.split("-")[0].strip()
                market_set = set_code.upper().strip()

                # 1. Store the unique card details safely
                cursor.execute("""
                    INSERT INTO Cards 
                    (UniqueCardId, CardNumber, CardName, Rarity, SetId, ImageUrl)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (UniqueCardId)
                    DO UPDATE SET
                        CardName = EXCLUDED.CardName,
                        ImageUrl = EXCLUDED.ImageUrl,
                        Rarity = EXCLUDED.Rarity;
                """, (unique_card_id, card_number, card_name, rarity, origin_set, img_url))

                # 2. Store the pricing tracking log
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
    initialize_database()

    print("Pre-building safety target list for all One Piece TCG sets...")
    baseline_sets = []
    
    baseline_sets += [f"op{str(i).zfill(2)}" for i in range(1, 16)] 
    baseline_sets += [f"eb{str(i).zfill(2)}" for i in range(1, 5)]   
    baseline_sets += [f"st{str(i).zfill(2)}" for i in range(1, 31)]  

    # DYNAMIC AUTO-DETECTION COUPLING
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