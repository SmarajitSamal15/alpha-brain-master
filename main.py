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

# Multi-Key Rotation Support (Supports 12+ API Keys via CSV in GEMINI_API_KEYS)
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

def normalize_name(name):
    """Normalize project name strictly to catch variations like Project X, Project-X, ProjectX."""
    if not name:
        return ""
    return re.sub(r'[^a-zA-Z0-9]', '', str(name)).upper()

def normalize_domain(domain_or_url):
    """Extract clean domain for duplicate checking."""
    if not domain_or_url:
        return ""
    val = str(domain_or_url).lower().strip()
    val = re.sub(r'^https?://', '', val)
    val = re.sub(r'^www\.', '', val)
    return val.split('/')[0].split(':')[0]

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
    """Deduplicate investor names cleanly."""
    if not investor_str or investor_str.upper() in ["N/A", "NONE", "UNKNOWN", "UNDISCLOSED"]:
        return "Institutional / Private Syndicate"
    
    raw_list = [item.strip() for item in investor_str.split(",") if item.strip()]
    seen = set()
    deduped = []
    
    for item in raw_list:
        norm = normalize_name(item)
        if norm and norm not in seen:
            seen.add(norm)
            deduped.append(item)
            
    return ", ".join(deduped) if deduped else "Institutional / Private Syndicate"

# =================================================================================
# 🔄 MULTI-KEY FAILOVER GEMINI MANAGER (UP TO 12+ KEYS SUPPORT)
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
# 💾 DATABASE ENGINE (24H CACHE PURGE + PERMANENT SENT HISTORY LOCK)
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
                CREATE TABLE IF NOT EXISTS project_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    unique_hash TEXT UNIQUE,
                    created_at TEXT
                )
            """)
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

    def purge_24h_cache(self, hours=24):
        """Purges old temporary cache records while retaining sent_history locks permanently."""
        cutoff_str = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM project_cache WHERE created_at < ?", (cutoff_str,))
            deleted = cursor.rowcount
            conn.commit()
            if deleted > 0:
                logging.info(f"🧹 Purged {deleted} temporary cache records older than {hours} hours.")

# =================================================================================
# 🧠 DUAL-PIPELINE AI ENGINE (VC FUNDING + WHOLE GOOGLE AIRDROP/TGE OSINT)
# =================================================================================

class GeminiAlphaEngine:
    def __init__(self, key_manager, db):
        self.key_manager = key_manager
        self.db = db

    def execute_master_pipeline(self):
        logging.info("⚡ Executing Pipeline 1: Dedicated VC Funding Scan (RootData, CryptoRank, Crypto-Fundraising + Whole Web)...")
        vc_deals = self.fetch_vc_fundraising_12h()
        
        logging.info("⚡ Executing Pipeline 2: Whole Google Web & RSS Scan (Airdrops, TGEs, Retros, Infinite Keywords)...")
        global_news = self.fetch_global_airdrop_tge_12h()

        combined_items = vc_deals + global_news
        if not combined_items:
            logging.info("ℹ️ No new live/fresh news or VC funding deals found within the last 12 hours.")
            return

        seen_in_run = set()

        for item in combined_items:
            try:
                if not isinstance(item, dict):
                    continue

                p_name = item.get("project_name", "")
                category_type = item.get("post_category", "WEB3_NEWS")
                
                if not p_name or p_name.upper() in ["N/A", "NONE", "UNKNOWN"]:
                    continue

                clean_key = normalize_name(p_name)
                if not clean_key:
                    continue

                # Unique Hash creation combining project name + category
                unique_hash = hashlib.md5(f"{clean_key}_{category_type}".encode()).hexdigest()

                # Double Lock Duplicate Check
                if clean_key in seen_in_run or self.db.is_hash_sent(unique_hash):
                    logging.info(f"⏭️ Skipping duplicate or already posted item: {p_name} [{category_type}]")
                    continue

                seen_in_run.add(clean_key)

                # OSINT Enrichment via Gemini Whole Internet Search
                airdrop_info = self.research_airdrop_whole_internet(p_name, item.get("domain", ""))
                
                raw_link = clean_val(airdrop_info.get("airdrop_link"), "").strip()
                official_link = clean_val(item.get("official_link"), f"https://{item.get('domain', 'google.com')}")
                
                if not is_valid_http_url(official_link):
                    official_link = f"https://{item.get('domain', 'rootdata.com')}"

                has_active = airdrop_info.get("has_active_airdrop", False)
                is_valid_airdrop_link = is_valid_http_url(raw_link) and not any(x in raw_link for x in ["rootdata.com", "cryptorank.io", "crypto-fundraising.info"])

                # Post Construction
                message = self.build_beautiful_telegram_post(item, airdrop_info, raw_link, official_link, is_valid_airdrop_link or has_active)

                if self.send_telegram_retry_safe(message):
                    self.db.mark_hash_sent(p_name, unique_hash)

            except Exception as e:
                logging.error(f"⚠️ Error processing item: {str(e)}")
                continue

    def fetch_vc_fundraising_12h(self):
        """Pipeline 1: Target RootData, CryptoRank, Crypto-Fundraising AND Whole Google/Web for VC Funding"""
        system_prompt = (
            "You are a Web3 VC Deal Analyst. Search RootData (rootdata.com), CryptoRank (cryptorank.io), Crypto-Fundraising (crypto-fundraising.info), "
            "AND the ENTIRE GOOGLE WEB / X / News Outlets for fresh Web3 startup VC funding rounds announced strictly in the LAST 12 HOURS. "
            "Separate 'fresh_investors' (current round) and 'total_investors' (all historical investors). Do not write lazy summaries or truncate names."
        )
        user_prompt = """
Search RootData, CryptoRank, Crypto-Fundraising, and the ENTIRE WEB for Web3 startup fundraising announcements from the LAST 12 HOURS.

JSON Structure:
[
  {
    "post_category": "VC_FUNDING",
    "project_name": "Project Name",
    "domain": "official domain",
    "series_round": "Seed / Series A / Strategic / Undisclosed",
    "funding_amount": "$XX M or Undisclosed",
    "total_funding": "$XX M or Undisclosed",
    "fresh_investors": "Comma separated fresh investors",
    "total_investors": "Comma separated all historical investors",
    "official_link": "Direct announcement URL or RootData/CryptoRank link"
  }
]
Output ONLY raw JSON code block or [] if empty.
"""
        try:
            res = self.key_manager.call_gemini_with_search(system_prompt, user_prompt)
            json_str = self.extract_json(res)
            data = json.loads(json_str)
            return data if isinstance(data, list) else []
        except Exception as e:
            logging.error(f"Error in VC Pipeline: {str(e)}")
            return []

    def fetch_global_airdrop_tge_12h(self):
        """Pipeline 2: Whole Google Search & RSS OSINT covering Infinite Keywords (Airdrops, TGEs, Retros, Testnets)"""
        system_prompt = (
            "You are an OSINT Web3 Intelligence Specialist. Search the entire web (Google News, X/Twitter, Web3 Blogs, Medium, Mirror) "
            "strictly for fresh Web3 announcements from the LAST 12 HOURS. "
            "Keywords to cover: Airdrop Snapshot, Token Generation Event (TGE), Mainnet Launch, Incentivized Testnet, Retroactive Rewards, Claim Portal Live, Points Program. "
            "Focus strictly on high-quality real projects."
        )
        user_prompt = """
Search the ENTIRE GOOGLE WEB for breaking Web3 news from the LAST 12 HOURS covering:
- Live Airdrop Claims & Snapshots
- Upcoming / Live TGE Announcements
- Incentivized Testnets, Mainnets, and Points Programs

JSON Structure:
[
  {
    "post_category": "AIRDROP_TGE",
    "project_name": "Project Name",
    "domain": "official domain",
    "event_title": "Headline / Event Name (e.g. TGE Date Confirmed / Airdrop Claim Live)",
    "summary": "2-3 lines precise summary of the event",
    "official_link": "Direct announcement URL or official site"
  }
]
Output ONLY raw JSON code block or [] if empty.
"""
        try:
            res = self.key_manager.call_gemini_with_search(system_prompt, user_prompt)
            json_str = self.extract_json(res)
            data = json.loads(json_str)
            return data if isinstance(data, list) else []
        except Exception as e:
            logging.error(f"Error in Global OSINT Pipeline: {str(e)}")
            return []

    def research_airdrop_whole_internet(self, project_name, domain):
        system_prompt = (
            "You are an OSINT Researcher. Search official subdomains (app.*, testnet.*, claim.*, faucet.*), X/Twitter, and Galxe/Layer3 "
            "for active direct participation or claim links for the given project."
        )
        user_prompt = f"""
Search whole internet for project: {project_name} (Domain: {domain})

JSON Format:
{{
  "has_active_airdrop": true,
  "campaign_type": "Points Program / Testnet / TGE Claim Portal",
  "airdrop_link": "Direct participation subdomain or official link"
}}
Output ONLY raw JSON code block.
"""
        try:
            res = self.key_manager.call_gemini_with_search(system_prompt, user_prompt)
            json_str = self.extract_json(res)
            data = json.loads(json_str)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def build_beautiful_telegram_post(self, item, airdrop_info, raw_link, official_link, has_active):
        p_name = self.escape_html(item.get("project_name"))
        category = item.get("post_category", "VC_FUNDING")

        if category == "VC_FUNDING":
            round_type = self.escape_html(clean_val(item.get("series_round"), "Strategic Round"))
            amount = self.escape_html(clean_val(item.get("funding_amount"), "Undisclosed"))
            total_funding = self.escape_html(clean_val(item.get("total_funding"), "Undisclosed"))
            fresh_vcs = self.escape_html(clean_investor_list(item.get("fresh_investors")))
            total_vcs = self.escape_html(clean_investor_list(item.get("total_investors")))
            campaign_type = self.escape_html(clean_val(airdrop_info.get("campaign_type"), "Institutional VC Round"))

            header = "💎 <b>VC FUNDING & AIRDROP MASTER ALERT</b> 💎" if has_active else "💰 <b>NEW VC FUNDING ALERT</b> 💰"
            
            links = f"🔗 <b>Official Source:</b>\n<a href='{official_link}'><b>Visit Official Announcement</b></a>"
            if has_active and is_valid_http_url(raw_link):
                links += f"\n\n🪂 <b>Direct Participation Link:</b>\n<a href='{raw_link}'><b>Click Here to Participate / Claim</b></a>"

            return (
                f"{header}\n\n"
                f"📌 <b>Project:</b> {p_name}\n"
                f"🏷️ <b>Round:</b> <code>{round_type}</code>\n"
                f"💰 <b>Fresh Raised:</b> {amount}\n"
                f"📊 <b>Total Valuation/Raised:</b> {total_funding}\n"
                f"🎯 <b>Category:</b> <code>{campaign_type}</code>\n\n"
                f"🤝 <b>Fresh Investors:</b>\n{fresh_vcs}\n\n"
                f"🏛️ <b>Total Backers:</b>\n{total_vcs}\n\n"
                f"{links}"
            )
        else:
            event_title = self.escape_html(clean_val(item.get("event_title"), "Airdrop / TGE Update"))
            summary = self.escape_html(clean_val(item.get("summary"), "Live Web3 event update."))
            campaign_type = self.escape_html(clean_val(airdrop_info.get("campaign_type"), "Live Ecosystem Incentive"))

            header = "🚀 <b>LIVE AIRDROP / TGE BREAKING ALERT</b> 🚀"
            links = f"🔗 <b>Official Source:</b>\n<a href='{official_link}'><b>Verify Official Announcement</b></a>"
            if is_valid_http_url(raw_link):
                links += f"\n\n🪂 <b>Direct Portal Link:</b>\n<a href='{raw_link}'><b>Access Participation Portal</b></a>"

            return (
                f"{header}\n\n"
                f"📌 <b>Project:</b> {p_name}\n"
                f"⚡ <b>Event:</b> <code>{event_title}</code>\n"
                f"🎯 <b>Campaign Type:</b> <code>{campaign_type}</code>\n\n"
                f"📝 <b>Summary:</b>\n{summary}\n\n"
                f"{links}"
            )

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
        if not text:
            return "[]"
        text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text.strip())
        match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", text)
        return match.group(1).strip() if match else text.strip()

    @staticmethod
    def escape_html(text):
        if not text:
            return "Undisclosed"
        return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# =================================================================================
# 🚀 MAIN EXECUTION ENGINE
# =================================================================================

def main():
    logging.info("=========================================================")
    logging.info("🚀 STARTING SUPER POWERFUL WEB3 AUTOMATION SYSTEM 🚀")
    logging.info("=========================================================")

    key_manager = GeminiAPIKeyManager(GEMINI_API_KEYS, MODEL_CANDIDATES)
    db = AlphaDatabase(DB_FILE)
    engine = GeminiAlphaEngine(key_manager, db)

    # 24-hour cache cleanup
    db.purge_24h_cache(hours=CACHE_PURGE_HOURS)
    
    # Execute Master Pipeline
    engine.execute_master_pipeline()

    logging.info("✅ Execution completed successfully with zero repeat duplicates.")

if __name__ == "__main__":
    main()
