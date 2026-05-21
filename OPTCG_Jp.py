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
        conn = create_connection()
        cursor = conn.cursor()

        print(f"Processing set: {set_code.upper()}")
        time.sleep(0.5)

        url = f"{BASE_URL}{set_code}"
        response = requests.get(url, headers=HEADERS, timeout=15)

        if response.status_code != 200:
            print(f"Skipping {url} (Status {response.status_code})")
            return 0, 0

        soup = BeautifulSoup(response.text, "html.parser")
        card_boxes = soup.find_all("div", class_="card-product")

        for card in card_boxes:
            try:
                # 1. Extract Card Name Safely
                title_element = card.find("h4", class_="text-primary")
                if not title_element:
                    continue
                card_name = title_element.text.strip()

                # 2. Extract Card Number Safely
                number_element = card.find("span", class_="border-dark")
                if not number_element:
                    number_element = card.find("span")
                    if not number_element or "-" not in number_element.text:
                        continue
                
                raw_number = number_element.text.strip().upper().replace(" ", "")
                if "-" not in raw_number:
                    if raw_number.startswith(("OP", "ST", "EB")) and len(raw_number) >= 7:
                        card_number = f"{raw_number[:4]}-{raw_number[4:]}"
                    else:
                        continue 
                else:
                    card_number = raw_number

                # 3. Extract Price Safely
                price_element = card.find("strong")
                if not price_element:
                    continue
                price_text = price_element.text.strip()
                price_jpy = int("".join(filter(str.isdigit, price_text)))

                # 4. FIX: TARGET ONLY THE GENUINE CARD IMAGES (Bypasses the Favorite Star SVG)
                img_url = ""
                alt_text = ""
                
                # Specifically search inside the product image frame wrapper class
                img_container = card.find("div", class_="product-img")
                img_tag = img_container.find("img") if img_container else card.find("img", alt=True)
                
                # Double check to prevent capturing favorite/bookmark system svgs
                if img_tag and img_tag.has_attr("src") and "star.svg" not in img_tag["src"]:
                    src_url = img_tag["src"]
                    alt_text = img_tag.get("alt", "").strip().upper()
                    if src_url.startswith("http"):
                        img_url = src_url
                    else:
                        img_url = f"https://card.yuyu-tei.jp{src_url}" if src_url.startswith("/") else f"https://{src_url}"
                
                # Image link builder fallback from detail links if the product is temporarily out-of-stock
                if not img_url or "star.svg" in img_url:
                    link_element = card.find("a", href=True)
                    if link_element:
                        detail_url = link_element["href"]
                        parts = detail_url.strip("/").split("/")
                        if len(parts) >= 5:
                            set_code_img = parts[-2]
                            image_id = parts[-1]
                            img_url = f"https://card.yuyu-tei.jp/opc/front/{set_code_img}/{image_id}.jpg"

                # =========================================================
                # 🛠️ CONTEXTUAL RARITY IDENTIFIER ENGINE
                # =========================================================
                context_rarity = ""
                chase_variant = ""

                # Build full string context out of everything available inside this block
                combined_text = f"{card_name.upper()} {alt_text} {card.text.upper()}"

                # Strategy A: Scan for exact label markers
                if "P-L" in combined_text or "P-LEADER" in combined_text:
                    context_rarity = "P-L"
                elif "P-SEC" in combined_text:
                    context_rarity = "P-SEC"
                elif "SEC" in combined_text:
                    context_rarity = "SEC"
                elif "P-SR" in combined_text:
                    context_rarity = "P-SR"
                elif "SR" in combined_text:
                    context_rarity = "SR"
                elif "P-R" in combined_text or "特別パラレル" in combined_text:
                    context_rarity = "P-R"
                elif "R" in combined_text:
                    context_rarity = "R"
                elif "L" in combined_text or "LEADER" in combined_text:
                    context_rarity = "L"

                # Strategy B: Fall back to checking parent header blocks (Standard Set Lists)
                if not context_rarity or context_rarity in ["C", "UNKNOWN"]:
                    parent_section = card.find_parent("div", class_="card-list-box")
                    if parent_section:
                        header = parent_section.find(["h3", "h4", "h5", "div"], class_="title")
                        if header:
                            header_clean = header.text.strip().replace("CARD LIST", "").replace(" ", "").upper()
                            if header_clean:
                                context_rarity = header_clean

                if not context_rarity:
                    context_rarity = "C"

                # Strategy C: Separate High-Value Special Variants
                if "手配書" in combined_text or "WANTED" in combined_text:
                    chase_variant = "_WANTED"
                    context_rarity = "SP"
                elif "レッド" in combined_text or "RED" in combined_text:
                    chase_variant = "_RED_MANGA"
                    context_rarity = "P-SEC"
                elif "スーパーパラレル" in combined_text or "SUPER" in combined_text or "コミック" in combined_text or "原作" in combined_text:
                    chase_variant = "_MANGA"
                    context_rarity = "P-SEC"

                # Fix up baseline parallels (e.g. R to P-R) if missing the explicit prefix
                if "パラレル" in combined_text and "P-" not in context_rarity and context_rarity not in ["SP", "L", "P-L"]:
                    if context_rarity in ["SEC", "SR", "R", "UC", "C"]:
                        context_rarity = f"P-{context_rarity}"

                # 5. Generate Unique Keys
                if chase_variant:
                    unique_card_id = f"{card_number}_{context_rarity}{chase_variant}"
                else:
                    unique_card_id = f"{card_number}_{context_rarity}"

                # Disambiguate baseline P-SEC from manga listings using filename endpoints
                if context_rarity == "P-SEC" and not chase_variant:
                    if img_url and ("10153" in img_url or "10154" in img_url):
                        pass 
                    else:
                        unique_card_id = f"{card_number}_P-SEC"

                origin_set = card_number.split("-")[0].strip()
                market_set = set_code.upper().strip()

                # 6. Database Transaction Commit Pipeline
                cursor.execute("""
                    INSERT INTO Cards 
                    (UniqueCardId, CardNumber, CardName, Rarity, SetId, ImageUrl)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (UniqueCardId)
                    DO UPDATE SET
                        CardName = EXCLUDED.CardName,
                        ImageUrl = EXCLUDED.ImageUrl,
                        Rarity = EXCLUDED.Rarity;
                """, (unique_card_id, card_number, card_name, context_rarity, origin_set, img_url))

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