import os
import re
import json
import time
import html
import sqlite3
import logging
import hashlib
import requests
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone

# =================================================================================
# ⚙️ SECURE ENVIRONMENT & MASTER CONFIGURATION
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
MIN_SCORE_THRESHOLD = 65  # Minimum score out of 100 to trigger alert

# Complete Auto-Switch Model Pipeline (Flash, Pro & Plain Model Candidates)
MODEL_CANDIDATES = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-2.0-flash-lite",
    "gemini-2.0-pro-exp-02-05"
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# =================================================================================
# 🧰 HELPER UTILITIES & SMART SANITIZERS
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
    """Deduplicate investor names cleanly and ignore generic placeholder values."""
    if not investor_str or str(investor_str).upper() in ["N/A", "NONE", "UNKNOWN", "UNDISCLOSED", "INSTITUTIONAL / PRIVATE SYNDICATE", ""]:
        return "Undisclosed"
    
    raw_list = [item.strip() for item in str(investor_str).split(",") if item.strip()]
    seen = set()
    deduped = []
    
    for item in raw_list:
        norm = normalize_text(item)
        if norm and norm not in seen:
            seen.add(norm)
            deduped.append(item)
            
    return ", ".join(deduped) if deduped else "Undisclosed"

def escape_html(text):
    """Safely escape HTML entities for Telegram HTML parse mode to prevent 400 Bad Request crashes."""
    if not text:
        return "Undisclosed"
    return html.escape(str(text))

def generate_smart_event_hash(project_name, event_title, event_type):
    """Smart Event-Specific Hash."""
    norm_pname = normalize_text(project_name)
    combined_text = f" {event_title} {event_type} ".upper()

    milestone_keywords = []
    milestone_map = [
        ("EXCHANGE_LAUNCH", ["CEX", "DEX", "EXCHANGE LAUNCH", "LAUNCHPOOL", "LAUNCHPAD", "TRADING COMPETITION", "LIQUIDITY MINING", "PERPETUAL", "SPOT"]),
        ("BONUS_GIVEAWAY", ["BONUS", "GIVEAWAY", "FUTURES", "STARTER", "WELCOME", "VOUCHER", "MYSTERY BOX", "REWARD", "CASINO", "NO-DEPOSIT"]),
        ("MINING_QUEST", ["MINING", "NODE", "TAP-TO-EARN", "QUEST", "FAUCET", "WHITELIST", "EARLY ACCESS"]),
        ("TESTNET", ["TESTNET", "DEVNET", "FAUCET", "QUEST"]),
        ("AIRDROP_CLAIM", ["AIRDROP", "CLAIM", "ELIGIBILITY", "DISTRIBUTION"]),
        ("TGE_SNAPSHOT", ["TGE", "SNAPSHOT", "TOKEN LAUNCH", "IEO", "IDO"]),
        ("PRE_SEED", ["PRE-SEED", "PRE SEED"]),
        ("SEED_ROUND", [" SEED ", "SEED ROUND"]),
        ("SERIES_A", ["SERIES A", "SERIES-A"]),
        ("SERIES_B", ["SERIES B", "SERIES-B"]),
        ("STRATEGIC_ROUND", ["STRATEGIC", "PRIVATE SALE", "EXTENSION", "RAISED"]),
    ]

    for label, keywords in milestone_map:
        if any(kw in combined_text for kw in keywords):
            milestone_keywords.append(label)

    if not milestone_keywords:
        clean_title = normalize_text(event_title)[:12]
        milestone_keywords.append(clean_title)

    event_signature = f"{norm_pname}_{'_'.join(sorted(set(milestone_keywords)))}"
    return hashlib.md5(event_signature.encode()).hexdigest()

def detect_link_label(url):
    """Intelligently detect source website domain to give human-readable anchor labels."""
    if not url or not isinstance(url, str):
        return "Verify Official Announcement"
    domain = urlparse(url).netloc.lower()
    if "x.com" in domain or "twitter.com" in domain:
        return "Verify Official X (Twitter) Post"
    elif "rootdata.com" in domain:
        return "Verify RootData Analytics Page"
    elif "cryptorank.io" in domain:
        return "Verify CryptoRank Listing Page"
    elif "mirror.xyz" in domain or "medium.com" in domain:
        return "Read Official Article Announcement"
    return "Verify Official Announcement"

# =================================================================================
# 🔄 MULTI-KEY & MULTI-MODEL AUTO-SWITCH FAILOVER MANAGER
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
        max_attempts = max(len(self.keys) * len(self.models) * 2, 12)

        while attempts < max_attempts:
            current_key = self.get_current_key()
            current_model = self.get_current_model()

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent?key={current_key}"
            headers = {"Content-Type": "application/json"}
            
            # Payload optimized to avoid 400 Bad Request when search tool is active
            payload = {
                "contents": [{"parts": [{"text": user_prompt}]}],
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "tools": [{"googleSearch": {}}],
                "generationConfig": {
                    "temperature": temperature
                }
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

                elif response.status_code == 404:
                    self.switch_to_next_model(reason=f"HTTP 404 Model {current_model} Not Found")

                elif response.status_code == 400:
                    logging.error(f"⚠️ HTTP 400 Bad Request Payload Error on {current_model}: {response.text}")
                    self.switch_to_next_model(reason=f"HTTP 400 Payload Error on {current_model}")

                elif response.status_code == 429:
                    backoff = min(2 ** (attempts % 4), 8)
                    logging.warning(f"⚠️ Key Rate Limited (429). Sleeping {backoff}s...")
                    time.sleep(backoff)
                    self.switch_to_next_key(reason="HTTP 429 Rate Limit")
                    if self.current_key_idx == 0:
                        self.switch_to_next_model(reason=f"All keys limited on model {current_model}")

                elif response.status_code in [401, 403]:
                    self.switch_to_next_key(reason=f"HTTP Status {response.status_code} Auth/Quota Error")

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
                    opportunity_score INTEGER,
                    risk_level TEXT,
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

    def mark_hash_sent(self, project_name, unique_hash, score=0, risk="LOW"):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            now_str = datetime.now(timezone.utc).isoformat()
            cursor.execute("""
                INSERT OR IGNORE INTO sent_history (project_name, unique_hash, opportunity_score, risk_level, sent_at)
                VALUES (?, ?, ?, ?, ?)
            """, (project_name, unique_hash, score, risk, now_str))
            conn.commit()

    def purge_expired_data(self, hours=24):
        cutoff_str = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sent_history WHERE sent_at < ?", (cutoff_str,))
            deleted = cursor.rowcount
            conn.commit()
            if deleted > 0:
                logging.info(f"🧹 Successfully purged {deleted} record(s) older than {hours} hours from Database.")

# =================================================================================
# 🧠 MASTER 100-POINT CRYPTO INTELLIGENCE OS ENGINE
# =================================================================================

class GeminiAlphaEngine:
    def __init__(self, key_manager, db):
        self.key_manager = key_manager
        self.db = db

    def execute_master_pipeline(self):
        logging.info("⚡ Purging database history older than 24 hours...")
        self.db.purge_expired_data(hours=CACHE_PURGE_HOURS)

        logging.info("⚡ Executing Full 100-Point Crypto Intelligence Scan...")
        raw_items = self.fetch_fresh_web3_intelligence_12h()

        if not raw_items:
            logging.info("ℹ️ No new live Web3 Airdrop, CEX/DEX, Bonus, Mining, or Funding items found within the last 12 hours.")
            return

        seen_in_run = set()

        for item in raw_items:
            try:
                if not isinstance(item, dict):
                    continue

                p_name = clean_val(item.get("project_name"))
                e_title = clean_val(item.get("event_title"))
                event_type = clean_val(item.get("event_type"), "AIRDROP_TESTNET")

                if p_name.upper() in ["UNDISCLOSED", "N/A", "NONE", "UNKNOWN"]:
                    continue

                norm_pname = normalize_text(p_name)
                if not norm_pname:
                    continue

                unique_hash = generate_smart_event_hash(p_name, e_title, event_type)

                if unique_hash in seen_in_run or self.db.is_hash_sent(unique_hash):
                    logging.info(f"⏭️ Skipping duplicate event for project: {p_name} | {e_title}")
                    continue

                seen_in_run.add(unique_hash)

                score = int(item.get("opportunity_score", 70))
                risk = str(item.get("risk_level", "LOW")).upper()

                if risk == "CRITICAL" or item.get("verdict") == "BLOCKED":
                    logging.warning(f"🛡️ BLOCKED: Project {p_name} flagged with CRITICAL risk. Alert suppressed.")
                    self.db.mark_hash_sent(p_name, unique_hash, score, "CRITICAL")
                    continue

                if score < MIN_SCORE_THRESHOLD:
                    logging.info(f"📉 Filtered: {p_name} score ({score}/100) below threshold ({MIN_SCORE_THRESHOLD}).")
                    continue

                source_link = clean_val(item.get("source_link"), "https://rootdata.com")
                direct_link = clean_val(item.get("official_direct_link"), source_link)

                if not is_valid_http_url(source_link):
                    source_link = "https://rootdata.com"
                if not is_valid_http_url(direct_link):
                    direct_link = source_link

                message = self.build_beautiful_telegram_post(item, source_link, direct_link)

                if self.send_telegram_retry_safe(message):
                    self.db.mark_hash_sent(p_name, unique_hash, score, risk)

            except Exception as e:
                logging.error(f"⚠️ Error processing item: {str(e)}")
                continue

    def fetch_fresh_web3_intelligence_12h(self):
        system_prompt = (
            "You are an OG Hidden Analyst & Master Crypto Intelligence Operating System. "
            "Search RootData (rootdata.com), CryptoRank (cryptorank.io), Crypto-Fundraising (crypto-fundraising.info), "
            "Web3 RSS feeds, Mirror, Telegram Announcements, and X/Twitter STRICTLY for live announcements from the LAST 12 HOURS:\n"
            "1. CEX/DEX Launches, Early Access, Launchpools, Launchpads, and Liquidity Mining\n"
            "2. Incentivized Airdrops, Points Programs, Quests, Faucets, Testnets, Node/App Mining, and Whitelists\n"
            "3. Free Welcome Bonuses, Futures Trading Bonuses, Starter Packs, and No-Deposit Promos\n"
            "4. Confirmed TGE Dates, Snapshots, Eligibility Checks, and Claim Portals\n"
            "5. Fresh Tier-1/Tier-2 VC Funding Rounds (Pre-Seed, Seed, Series A/B, Strategic)\n\n"
            "EVALUATE EACH EVENT USING THE 100-POINT OPPORTUNITY MATRIX:\n"
            "- Fundamentals (0-20)\n"
            "- On-Chain / Smart Money (0-20)\n"
            "- VC Quality & Funding (0-15)\n"
            "- Catalyst Potential (0-15)\n"
            "- Tokenomics & Unlocks (0-10)\n"
            "- Market Structure (0-10)\n"
            "- Narrative Alignment (0-5)\n"
            "- Security & Risk (0-5)\n\n"
            "MANDATES:\n"
            "- ALWAYS state active user benefit in 'active_user_benefit'.\n"
            "- Classify 'event_type' into ONE of: ['EXCHANGE_LAUNCH', 'BONUS_GIVEAWAY', 'MINING_QUEST', 'VC_FUNDING', 'AIRDROP_TESTNET', 'TGE_SNAPSHOT'].\n"
            "- Assign 'risk_level': LOW, MEDIUM, HIGH, or CRITICAL.\n"
            "- Assign 'evidence_tier': Level 0 to Level 6.\n"
            "- Assign 'verdict': ELITE, VERY STRONG, STRONG, WATCH, IGNORE, or BLOCKED.\n"
            "- OUTPUT MUST BE VALID JSON ARRAY CODE BLOCK."
        )
        user_prompt = """
Scan breaking Web3 developments from the LAST 12 HOURS and return formatted JSON array.

JSON Output Schema:
[
  {
    "project_name": "Official Project Name",
    "event_title": "Descriptive Event Title in Flawless English",
    "event_type": "EXCHANGE_LAUNCH | BONUS_GIVEAWAY | MINING_QUEST | VC_FUNDING | AIRDROP_TESTNET | TGE_SNAPSHOT",
    "opportunity_score": 85,
    "confidence_score": 90,
    "evidence_tier": "Level 5",
    "risk_level": "LOW",
    "verdict": "ELITE",
    "series_round": "Pre-Seed / Seed / Series A / Testnet / Bonus / Undisclosed",
    "fresh_funding": "$XX M or Undisclosed",
    "total_funding": "$XX M or Undisclosed",
    "valuation": "$XX M or Undisclosed",
    "fresh_investors": "Comma separated fresh round investors or Undisclosed",
    "total_investors": "Comma separated historical backers or Undisclosed",
    "active_user_benefit": "Exact reward/action (e.g., $50 Free Bonus / Testnet Points / App Mining)",
    "official_direct_link": "Direct participation or claim link",
    "source_link": "Official announcement or RootData URL",
    "executive_summary": "2-3 precise sentences detailing utility, features, and participation steps."
  }
]
Return ONLY a valid JSON array or [] if no fresh data found.
"""
        try:
            res = self.key_manager.call_gemini_with_search(system_prompt, user_prompt)
            json_str = self.extract_json(res)
            data = json.loads(json_str)
            return data if isinstance(data, list) else []
        except Exception as e:
            logging.error(f"Error in Intelligence Scan: {str(e)}")
            return []

    def build_beautiful_telegram_post(self, item, source_link, direct_link):
        p_name = escape_html(clean_val(item.get("project_name"), "Web3 Project"))
        e_title = escape_html(clean_val(item.get("event_title"), "Breaking Update"))
        round_type = escape_html(clean_val(item.get("series_round"), "Milestone Phase"))
        event_type = str(item.get("event_type", "AIRDROP_TESTNET")).upper()

        score = item.get("opportunity_score", 70)
        confidence = item.get("confidence_score", 80)
        tier = escape_html(clean_val(item.get("evidence_tier"), "Level 3"))
        risk = escape_html(clean_val(item.get("risk_level"), "LOW"))
        verdict = escape_html(clean_val(item.get("verdict"), "STRONG"))

        fresh_raised = escape_html(clean_val(item.get("fresh_funding"), "Undisclosed"))
        total_raised = escape_html(clean_val(item.get("total_funding"), "Undisclosed"))
        valuation = escape_html(clean_val(item.get("valuation"), "Undisclosed"))

        fresh_vcs = escape_html(clean_investor_list(item.get("fresh_investors")))
        total_vcs = escape_html(clean_investor_list(item.get("total_investors")))
        summary = escape_html(clean_val(item.get("executive_summary"), "New opportunity update logged."))
        user_benefit = escape_html(clean_val(item.get("active_user_benefit"), "None"))

        check_text = (e_title + " " + round_type + " " + event_type + " " + user_benefit).lower()

        if any(kw in check_text for kw in ["cex", "dex", "exchange launch", "launchpool", "perpetual"]) or event_type == "EXCHANGE_LAUNCH":
            header = "🏛️ <b>NEW CEX/DEX LAUNCH & EXCHANGE CAMPAIGN</b>"
            is_vc_post = False
        elif any(kw in check_text for kw in ["bonus", "futures", "giveaway", "voucher", "no-deposit"]) or event_type == "BONUS_GIVEAWAY":
            header = "🎁 <b>EXCLUSIVE FREE BONUS & REWARD ALERT</b>"
            is_vc_post = False
        elif any(kw in check_text for kw in ["mining", "node", "tap-to-earn", "faucet", "quest"]) or event_type == "MINING_QUEST":
            header = "⛏️ <b>FREE MINING & ACTIONABLE AIRDROP ALERT</b>"
            is_vc_post = False
        elif any(kw in check_text for kw in ["tge", "snapshot", "token launch"]) or event_type == "TGE_SNAPSHOT":
            header = "🔥 <b>BREAKING TGE & SNAPSHOT ALERT</b>"
            is_vc_post = False
        elif any(kw in check_text for kw in ["seed", "series", "raised", "funding", "vc"]) or event_type == "VC_FUNDING":
            header = "💎 <b>NEW VC FUNDING & INSTITUTIONAL ALERT</b>"
            is_vc_post = True
        else:
            header = "🚀 <b>LIVE AIRDROP & TESTNET ALERT</b>"
            is_vc_post = False

        has_real_financial_data = any(val != "Undisclosed" for val in [fresh_raised, total_raised, valuation, fresh_vcs, total_vcs])

        funding_section = ""
        if is_vc_post or has_real_financial_data:
            funding_section = (
                f"💰 <b>FUNDING & VALUATION</b>\n"
                f"• <b>Stage:</b> <code>{round_type}</code>\n"
                f"• <b>Fresh Raised:</b> {fresh_raised}\n"
                f"• <b>Total Capital Raised:</b> {total_raised}\n"
                f"• <b>Valuation:</b> {valuation}\n\n"
                f"🤝 <b>KEY INVESTORS & BACKERS</b>\n"
                f"• <b>Fresh Investors:</b> {fresh_vcs}\n"
                f"• <b>Total Investors:</b> {total_vcs}\n\n"
            )

        benefit_section = ""
        if user_benefit != "None" and user_benefit.upper() != "UNDISCLOSED":
            benefit_section = (
                f"🎯 <b>CLAIMABLE BENEFIT & ACTION STEPS</b>\n"
                f"• {user_benefit}\n\n"
            )

        source_label = escape_html(detect_link_label(source_link))
        safe_source_link = html.escape(source_link, quote=True)
        safe_direct_link = html.escape(direct_link, quote=True)

        link_block = f"🔗 <b>Source Announcement:</b>\n<a href='{safe_source_link}'><b>{source_label}</b></a>"
        if direct_link != source_link and is_valid_http_url(direct_link):
            link_block += f"\n\n🪂 <b>Official Direct Claim Link:</b>\n<a href='{safe_direct_link}'><b>Click Here to Claim / Join Portal</b></a>"

        post_content = (
            f"{header}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Project:</b> {p_name}\n"
            f"📢 <b>Event:</b> <code>{e_title}</code>\n\n"
            f"🧬 <b>OG INTELLIGENCE METRICS</b>\n"
            f"• <b>Opportunity Score:</b> <code>{score}/100</code> ({verdict})\n"
            f"• <b>Confidence Rating:</b> {confidence}% (Tier: {tier})\n"
            f"• <b>Security Risk:</b> <code>{risk}</code>\n\n"
            f"{funding_section}"
            f"{benefit_section}"
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
                elif res.status_code == 400:
                    logging.error(f"❌ Telegram Parse Error (400): {res.text}")
                    return False
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
        if match:
            return match.group(1).strip()
        return "[]"

# =================================================================================
# 🚀 MAIN EXECUTION ENGINE
# =================================================================================

def main():
    logging.info("=========================================================")
    logging.info("🚀 STARTING MASTER ULTIMATE CRYPTO INTELLIGENCE BOT 🚀")
    logging.info("=========================================================")

    key_manager = GeminiAPIKeyManager(GEMINI_API_KEYS, MODEL_CANDIDATES)
    db = AlphaDatabase(DB_FILE)
    engine = GeminiAlphaEngine(key_manager, db)

    engine.execute_master_pipeline()

    logging.info("✅ Master pipeline execution completed cleanly.")

if __name__ == "__main__":
    main()
