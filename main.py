import os
import re
import json
import time
import sqlite3
import logging
import hashlib
import requests
from urllib.parse import urlparse
from datetime import datetime, timedelta

# =================================================================================
# ⚙️ SECURE ENVIRONMENT & SYSTEM CONFIGURATION
# =================================================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@AirdropHeadDepartmentAILabs")

# Multi-Key Rotation Support (CSV list in GEMINI_API_KEYS)
GEMINI_KEYS_RAW = os.getenv(
    "GEMINI_API_KEYS",
    os.getenv("GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", ""))
)
GEMINI_API_KEYS = [k.strip() for k in GEMINI_KEYS_RAW.split(",") if k.strip()]

DB_FILE = "alpha_brain_master.db"
TIME_WINDOW_HOURS = 12
CACHE_PURGE_HOURS = 24

MODEL_CANDIDATES = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash"
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# =================================================================================
# 🧰 HELPER UTILITIES
# =================================================================================

def clean_val(val, fallback="Undisclosed"):
    if not val or str(val).strip().upper() in ["N/A", "NONE", "UNKNOWN", "NULL", "UNDEFINED", ""]:
        return fallback
    return str(val).strip()

def normalize_text(text):
    """Normalize text string for strict comparison and hashing."""
    if not text:
        return ""
    return re.sub(r'[^a-zA-Z0-9]', '', str(text)).upper()

def is_valid_http_url(url):
    """Strict HTTP/HTTPS URL syntax validation."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme in ["http", "https"] and bool(parsed.netloc)
    except Exception:
        return False

def clean_investor_list(investor_str):
    """Deduplicate investor names cleanly and present concise, accurate lists."""
    if not investor_str or str(investor_str).upper() in ["N/A", "NONE", "UNKNOWN", "UNDISCLOSED", ""]:
        return "Institutional / Private Syndicate"
    
    raw_list = [item.strip() for item in str(investor_str).split(",") if item.strip()]
    seen = set()
    deduped = []
    
    for item in raw_list:
        norm = normalize_text(item)
        if norm and norm not in seen:
            seen.add(norm)
            deduped.append(item)
            
    return ", ".join(deduped) if deduped else "Institutional / Private Syndicate"

def escape_html(text):
    """Safely escape HTML entities for Telegram message dispatch."""
    if not text:
        return "Undisclosed"
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def generate_smart_event_hash(project_name, event_title, series_round):
    """
    Smart Event-Specific Hash:
    - Blocks exact duplicate posts for the SAME event.
    - ALLOWS different news updates for the SAME project.
    """
    norm_pname = normalize_text(project_name)
    combined_text = f"{event_title} {series_round}".upper()

    milestone_keywords = []
    milestone_map = [
        ("MAINNET", ["MAINNET"]),
        ("TESTNET", ["TESTNET", "DEVNET", "FAUCET", "QUEST"]),
        ("AIRDROP_CLAIM", ["AIRDROP", "CLAIM", "ELIGIBILITY", "DISTRIBUTION"]),
        ("TGE_SNAPSHOT", ["TGE", "SNAPSHOT", "TOKEN LAUNCH", "LISTING"]),
        ("SEED_ROUND", ["SEED"]),
        ("SERIES_A", ["SERIES A", "SERIES-A"]),
        ("SERIES_B", ["SERIES B", "SERIES-B"]),
        ("STRATEGIC_ROUND", ["STRATEGIC", "PRIVATE SALE", "RAISED"]),
    ]

    for label, keywords in milestone_map:
        if any(kw in combined_text for kw in keywords):
            milestone_keywords.append(label)

    if not milestone_keywords:
        clean_title = normalize_text(event_title)[:12]
        milestone_keywords.append(clean_title)

    event_signature = f"{norm_pname}_{'_'.join(sorted(set(milestone_keywords)))}"
    return hashlib.md5(event_signature.encode()).hexdigest()

# =================================================================================
# 🔄 MULTI-KEY FAILOVER GEMINI MANAGER
# =================================================================================

class GeminiAPIKeyManager:
    def __init__(self, keys, models):
        self.keys = keys
        self.models = models
        self.current_key_idx = 0
        self.current_model_idx = 0

    def get_current_key(self):
        if not self.keys:
            raise Exception("❌ CRITICAL: No Gemini API keys provided in GEMINI_API_KEYS environment variable.")
        return self.keys[self.current_key_idx]

    def get_current_model(self):
        return self.models[self.current_model_idx]

    def switch_to_next_key(self, reason="Rate Limit / Failover"):
        if not self.keys:
            return
        old_idx = self.current_key_idx
        self.current_key_idx = (self.current_key_idx + 1) % len(self.keys)
        logging.warning(f"🔄 Rotating Gemini API Key ({old_idx + 1}/{len(self.keys)} -> {self.current_key_idx + 1}/{len(self.keys)}) Reason: [{reason}]")

    def switch_to_next_model(self, reason="Model Fallback"):
        if len(self.models) <= 1:
            return
        old_model = self.get_current_model()
        self.current_model_idx = (self.current_model_idx + 1) % len(self.models)
        new_model = self.get_current_model()
        logging.warning(f"🔀 Switching Model ({old_model} -> {new_model}) Reason: [{reason}]")

    def call_gemini_with_search(self, system_prompt, user_prompt, temperature=0.1):
        attempts = 0
        max_attempts = max(len(self.keys) * len(self.models) * 2, 10)

        while attempts < max_attempts:
            current_key = self.get_current_key()
            current_model = self.get_current_model()

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent?key={current_key}"
            headers = {"Content-Type": "application/json"}
            payload = {
                "contents": [{"parts": [{"text": user_prompt}]}],
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "tools": [{"googleSearch": {}}],
                "generationConfig": {"temperature": temperature}
            }

            try:
                response = requests.post(url, headers=headers, json=payload, timeout=60)

                if response.status_code == 200:
                    data = response.json()
                    candidates = data.get("candidates", [])
                    if candidates and "content" in candidates[0]:
                        parts = candidates[0]["content"].get("parts", [])
                        for part in parts:
                            if "text" in part:
                                return part["text"]

                elif response.status_code == 429:
                    backoff = min(2 ** (attempts % 4), 8)
                    logging.warning(f"⚠️ Key Rate Limited (429). Sleeping {backoff}s...")
                    time.sleep(backoff)
                    self.switch_to_next_key(reason="HTTP 429 Rate Limit")
                    if self.current_key_idx == 0:
                        self.switch_to_next_model(reason=f"All keys limited on model {current_model}")

                elif response.status_code in [401, 403]:
                    self.switch_to_next_key(reason=f"HTTP Status {response.status_code} Auth/Quota Error")

                elif response.status_code == 404:
                    self.switch_to_next_model(reason="HTTP 404 Model Not Found")

                else:
                    backoff = min(2 ** (attempts % 3), 6)
                    time.sleep(backoff)
                    self.switch_to_next_key(reason=f"Server Error {response.status_code}")

            except requests.exceptions.Timeout:
                logging.warning("⏳ Network timeout. Rotating key...")
                self.switch_to_next_key(reason="Network Timeout")
            except Exception as e:
                logging.error(f"⚠️ Exception contacting Gemini: {str(e)}")
                self.switch_to_next_key(reason="Network Exception")

            attempts += 1
            time.sleep(1.2)

        raise Exception("❌ Exhausted all Gemini API keys and model fallbacks.")

# =================================================================================
# 💾 DATABASE ENGINE (EVENT-SPECIFIC DUPLICATE STOP + AUTO 24H PURGE)
# =================================================================================

class AlphaDatabase:
    def __init__(self, db_path):
        self.db_path = db_path
        self.init_db()

    def get_connection(self):
        return sqlite3.connect(self.db_path)

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sent_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_name TEXT,
                    unique_hash TEXT UNIQUE,
                    sent_at TEXT
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sent_hash ON sent_history(unique_hash)")
            conn.commit()

    def is_hash_sent(self, unique_hash):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM sent_history WHERE unique_hash = ?", (unique_hash,))
            return cursor.fetchone() is not None

    def mark_hash_sent(self, project_name, unique_hash):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            now_str = datetime.utcnow().isoformat()
            cursor.execute("""
                INSERT OR IGNORE INTO sent_history (project_name, unique_hash, sent_at)
                VALUES (?, ?, ?)
            """, (project_name, unique_hash, now_str))
            conn.commit()

    def purge_expired_data(self, hours=24):
        """Purge data older than 24 hours to prevent DB bloat."""
        cutoff_str = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sent_history WHERE sent_at < ?", (cutoff_str,))
            deleted = cursor.rowcount
            conn.commit()
            if deleted > 0:
                logging.info(f"🧹 Successfully purged {deleted} record(s) older than {hours} hours from Database.")

# =================================================================================
# 🧠 DUAL INTELLIGENCE SCANNER ENGINE
# =================================================================================

class GeminiAlphaEngine:
    def __init__(self, key_manager, db):
        self.key_manager = key_manager
        self.db = db

    def execute_master_pipeline(self):
        logging.info("⚡ Purging database history older than 24 hours...")
        self.db.purge_expired_data(hours=CACHE_PURGE_HOURS)

        logging.info("⚡ Executing Multi-Source Web3 Intelligence Scan...")
        raw_items = self.fetch_fresh_web3_intelligence_12h()

        if not raw_items:
            logging.info("ℹ️ No new live Web3 items found within the last 12 hours.")
            return

        seen_in_run = set()

        for item in raw_items:
            try:
                if not isinstance(item, dict):
                    continue

                p_name = clean_val(item.get("project_name"))
                e_title = clean_val(item.get("event_title"))
                round_type = clean_val(item.get("series_round"))

                if p_name.upper() in ["UNDISCLOSED", "N/A", "NONE", "UNKNOWN"]:
                    continue

                norm_pname = normalize_text(p_name)
                if not norm_pname:
                    continue

                unique_hash = generate_smart_event_hash(p_name, e_title, round_type)

                if unique_hash in seen_in_run or self.db.is_hash_sent(unique_hash):
                    logging.info(f"⏭️ Skipping duplicate event for project: {p_name} | {e_title}")
                    continue

                seen_in_run.add(unique_hash)

                source_link = clean_val(item.get("source_link"), "https://rootdata.com")
                direct_link = clean_val(item.get("official_direct_link"), source_link)

                if not is_valid_http_url(source_link):
                    source_link = "https://rootdata.com"
                if not is_valid_http_url(direct_link):
                    direct_link = source_link

                message = self.build_beautiful_telegram_post(item, source_link, direct_link)

                if self.send_telegram_retry_safe(message):
                    self.db.mark_hash_sent(p_name, unique_hash)

            except Exception as e:
                logging.error(f"⚠️ Error processing item: {str(e)}")
                continue

    def fetch_fresh_web3_intelligence_12h(self):
        """Scans RootData, CryptoRank, Crypto-Fundraising, RSS Feeds, X, and Web for fresh 12h Web3 News."""
        system_prompt = (
            "You are an Elite Web3 Intelligence Specialist. Search RootData (rootdata.com), CryptoRank (cryptorank.io), "
            "Crypto-Fundraising (crypto-fundraising.info), Web3 RSS feeds, Mirror, X/Twitter, and global web sources "
            "STRICTLY for breaking Web3 news, fresh funding rounds, live airdrops, TGEs, snapshots, and incentivized testnets announced within the LAST 12 HOURS.\n\n"
            "CRITICAL MANDATES:\n"
            "1. Output exact, grammatically flawless, and technically precise word-for-word information.\n"
            "2. Separate 'fresh_funding' (Fresh Raised in this round), 'total_funding' (Total Capital Raised historically), and 'valuation' cleanly into distinct JSON keys.\n"
            "3. Separate 'fresh_investors' (lead/fresh round backers) and 'total_investors' (all historic backers).\n"
            "4. Provide 'official_direct_link' (action portal, claim link, app, or testnet) AND 'source_link' (official announcement, tweet, or RootData link).\n"
            "5. OUTPUT ONLY VALID JSON CODE BLOCK OR []. DO NOT ADD ANY INTRODUCTORY OR CONVERSATIONAL TEXT."
        )
        user_prompt = """
Search RootData, CryptoRank, Crypto-Fundraising, RSS feeds, and the whole Internet for breaking Web3 announcements from the LAST 12 HOURS.

JSON Output Schema:
[
  {
    "project_name": "Exact Project Name",
    "event_title": "Short Descriptive Event Title (e.g., $15M Series A Raised / TGE Date Confirmed / Airdrop Claim Portal Live)",
    "series_round": "Seed / Series A / Strategic / TGE / Airdrop Claim",
    "fresh_funding": "$XX M or Undisclosed",
    "total_funding": "$XX M or Undisclosed",
    "valuation": "$XX M or Undisclosed",
    "fresh_investors": "Comma separated fresh round investors",
    "total_investors": "Comma separated all historical backers",
    "official_direct_link": "Direct participation subdomain, claim portal, testnet link, or official site",
    "source_link": "Direct official announcement URL, tweet, or RootData page",
    "executive_summary": "2-3 precise sentences detailing project utility, event scope, and immediate action steps."
  }
]
Return ONLY a valid JSON array block or [] if no fresh data found.
"""
        try:
            res = self.key_manager.call_gemini_with_search(system_prompt, user_prompt)
            json_str = self.extract_json(res)
            data = json.loads(json_str)
            return data if isinstance(data, list) else []
        except Exception as e:
            logging.error(f"Error in Multi-Source Intelligence Scan: {str(e)}")
            return []

    def build_beautiful_telegram_post(self, item, source_link, direct_link):
        """Generates a clean Telegram post with Dynamic Header and NO Category lines."""
        p_name = escape_html(clean_val(item.get("project_name"), "Web3 Project"))
        e_title = escape_html(clean_val(item.get("event_title"), "Breaking Update"))
        round_type = escape_html(clean_val(item.get("series_round"), "Institutional Phase"))
        
        fresh_raised = escape_html(clean_val(item.get("fresh_funding"), "Undisclosed"))
        total_raised = escape_html(clean_val(item.get("total_funding"), "Undisclosed"))
        valuation = escape_html(clean_val(item.get("valuation"), "Undisclosed"))
        
        fresh_vcs = escape_html(clean_investor_list(item.get("fresh_investors")))
        total_vcs = escape_html(clean_investor_list(item.get("total_investors")))
        summary = escape_html(clean_val(item.get("executive_summary"), "New ecosystem milestone and institutional update recorded."))

        check_text = (e_title + " " + round_type).lower()
        if any(kw in check_text for kw in ["airdrop", "testnet", "quest", "claim", "points", "faucet"]):
            header = "🚀 <b>LIVE AIRDROP & TESTNET ALERT</b> 🚀"
        elif any(kw in check_text for kw in ["tge", "snapshot", "token launch", "listing"]):
            header = "🔥 <b>BREAKING TGE & SNAPSHOT ALERT</b> 🔥"
        else:
            header = "💎 <b>NEW VC FUNDING & INSTITUTIONAL ALERT</b> 💎"

        link_block = f"🔗 <b>Source Announcement:</b>\n<a href='{source_link}'><b>Verify Official Announcement</b></a>"
        if direct_link != source_link and is_valid_http_url(direct_link):
            link_block += f"\n\n🪂 <b>Official Direct Participation Link:</b>\n<a href='{direct_link}'><b>Click Here to Participate / Access</b></a>"

        post_content = (
            f"{header}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Project:</b> {p_name}\n"
            f"📢 <b>Event:</b> <code>{e_title}</code>\n\n"
            f"💰 <b>FUNDING & VALUATION</b>\n"
            f"• <b>Stage / Round:</b> <code>{round_type}</code>\n"
            f"• <b>Fresh Raised:</b> {fresh_raised}\n"
            f"• <b>Total Capital Raised:</b> {total_raised}\n"
            f"• <b>Valuation:</b> {valuation}\n\n"
            f"🤝 <b>KEY INVESTORS & BACKERS</b>\n"
            f"• <b>Fresh Investors:</b> {fresh_vcs}\n"
            f"• <b>Total Investors:</b> {total_vcs}\n\n"
            f"📝 <b>EXECUTIVE SUMMARY</b>\n"
            f"{summary}\n\n"
            f"{link_block}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 <i>Powered by @AirdropHeadDepartment AI Engine</i>"
        )
        return post_content

    def send_telegram_retry_safe(self, message):
        if not TELEGRAM_BOT_TOKEN:
            logging.error("TELEGRAM_BOT_TOKEN missing.")
            return False

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        
        for attempt in range(3):
            try:
                res = requests.post(url, json=payload, timeout=12)
                if res.status_code == 200 and res.json().get("ok"):
                    logging.info("⚡ Telegram Post Successfully Dispatched.")
                    return True
                elif res.status_code == 429:
                    retry_after = int(res.json().get("parameters", {}).get("retry_after", 5))
                    time.sleep(retry_after)
                else:
                    time.sleep(2)
            except Exception as e:
                logging.error(f"Telegram Dispatch Error: {str(e)}")
                time.sleep(2)
        return False

    @staticmethod
    def extract_json(text):
        """Robustly extract JSON block or fallback to empty JSON array to prevent crash."""
        if not text:
            return "[]"
        text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text.strip())
        match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", text)
        if match:
            return match.group(1).strip()
        return "[]"

# =================================================================================
# 🚀 MAIN EXECUTION ENGINE
# =================================================================================

def main():
    logging.info("=========================================================")
    logging.info("🚀 STARTING 100/100 PRODUCTION WEB3 AUTOMATION SYSTEM 🚀")
    logging.info("=========================================================")

    key_manager = GeminiAPIKeyManager(GEMINI_API_KEYS, MODEL_CANDIDATES)
    db = AlphaDatabase(DB_FILE)
    engine = GeminiAlphaEngine(key_manager, db)

    # Execute Pipeline
    engine.execute_master_pipeline()

    logging.info("✅ Execution completed with smart event deduplication and clean dynamic formatting.")

if __name__ == "__main__":
    main()
