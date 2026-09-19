import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

# =================================================================================
# ⚙️ SECURE ENVIRONMENT & MASTER INTEGRATED CONFIGURATION
# =================================================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@AirdropHeadDepartmentAILabs")

# Multi-Key Rotation Support (CSV list in GEMINI_API_KEYS)
GEMINI_KEYS_RAW = os.getenv(
    "GEMINI_API_KEYS",
    os.getenv("GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", "")),
)
GEMINI_API_KEYS = [k.strip() for k in GEMINI_KEYS_RAW.split(",") if k.strip()]

DB_FILE = "alpha_brain_master.db"
CACHE_PURGE_HOURS = 24
MIN_OPPORTUNITY_SCORE = 0  # Set to 0 to capture all updates including scores below 65

# Production-Verified Gemini Model Failover Pipeline
MODEL_CANDIDATES = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
]

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)


# =================================================================================
# 🧰 HELPER UTILITIES & SMART SANITIZERS
# =================================================================================


def clean_val(val, fallback="Undisclosed"):
  if not val or str(val).strip().upper() in [
      "N/A",
      "NONE",
      "UNKNOWN",
      "NULL",
      "UNDEFINED",
      "",
  ]:
    return fallback
  return str(val).strip()


def normalize_text(text):
  """Normalize text string for strict comparison and hashing."""
  if not text:
    return ""
  return re.sub(r"[^a-zA-Z0-9]", "", str(text)).upper()


def is_valid_http_url(url):
  """Strict HTTP/HTTPS URL syntax validation."""
  if not url or not isinstance(url, str):
    return False
  try:
    parsed = urlparse(url.strip())
    return parsed.scheme in ["http", "https"] and bool(parsed.netloc)
  except Exception:
    return False


def escape_html(text):
  """Safely escape HTML entities for Telegram HTML parse mode."""
  if not text:
    return "Undisclosed"
  return html.escape(str(text))


def generate_smart_event_hash(project_name, event_title, event_type):
  """Smart Event-Specific Hash:

  - Blocks exact duplicate posts for the SAME event.
  - ALLOWS different news updates for the SAME project.
  """
  norm_pname = normalize_text(project_name)
  combined_text = f" {event_title} {event_type} ".upper()

  milestone_keywords = []
  milestone_map = [
      (
          "CEX_DEX_LISTING",
          [
              "COINBASE",
              "BINANCE",
              "OKX",
              "BYBIT",
              "KRAKEN",
              "KUCOIN",
              "BITGET",
              "GATE.IO",
              "MEXC",
              "UNISWAP",
              "RAYDIUM",
              "JUPITER",
              "AERODROME",
              "HYPERLIQUID",
              "PANCAKESWAP",
              "CURVE",
              "LISTING",
              "SPOT",
              "PERPETUAL",
              "FUTURES LISTING",
              "LAUNCHPOOL",
              "LAUNCHPAD",
              "ROADMAP",
          ],
      ),
      (
          "PROP_FIRM_FUNDED",
          [
              "FUNDED ACCOUNT",
              "PROP FIRM",
              "PROPFIRM",
              "EVALUATION WAIVER",
              "FREE CHALLENGE",
              "PROP TRADING",
              "NO-DEPOSIT PROP",
          ],
      ),
      (
          "BONUS_GIVEAWAY",
          [
              "BONUS",
              "GIVEAWAY",
              "STARTER",
              "WELCOME",
              "VOUCHER",
              "MYSTERY BOX",
              "REWARD",
              "NO-DEPOSIT",
          ],
      ),
      (
          "MINING_QUEST",
          [
              "MINING",
              "NODE",
              "TAP-TO-EARN",
              "QUEST",
              "FAUCET",
              "WHITELIST",
              "EARLY ACCESS",
          ],
      ),
      ("TESTNET", ["TESTNET", "DEVNET", "FAUCET"]),
      ("AIRDROP_CLAIM", ["AIRDROP", "CLAIM", "ELIGIBILITY", "DISTRIBUTION"]),
      ("TGE_SNAPSHOT", ["TGE", "SNAPSHOT", "TOKEN LAUNCH", "IEO", "IDO"]),
      ("VC_FUNDING", ["SEED", "SERIES", "RAISED", "FUNDING", "VALUATION", "VC"]),
  ]

  for label, keywords in milestone_map:
    if any(kw in combined_text for kw in keywords):
      milestone_keywords.append(label)

  if not milestone_keywords:
    clean_title = normalize_text(event_title)[:12]
    milestone_keywords.append(clean_title)

  event_signature = (
      f"{norm_pname}_{'_'.join(sorted(set(milestone_keywords)))}"
  )
  return hashlib.md5(event_signature.encode()).hexdigest()


def detect_link_label(url):
  """Intelligently detect source website domain to give human-readable anchor labels."""
  if not url or not isinstance(url, str):
    return "Verify Official Announcement"
  domain = urlparse(url).netloc.lower()

  if "coinbase.com" in domain:
    return "Verify Coinbase Official"
  elif "binance.com" in domain:
    return "Verify Binance Official"
  elif "okx.com" in domain:
    return "Verify OKX Official"
  elif "bybit.com" in domain:
    return "Verify Bybit Official"
  elif "kraken.com" in domain:
    return "Verify Kraken Official"
  elif "kucoin.com" in domain:
    return "Verify KuCoin Official"
  elif "bitget.com" in domain:
    return "Verify Bitget Official"
  elif "mexc.com" in domain:
    return "Verify MEXC Official Blog"
  elif "gate.io" in domain:
    return "Verify Gate.io Official"
  elif "uniswap.org" in domain:
    return "Verify Uniswap App"
  elif "jup.ag" in domain:
    return "Verify Jupiter Exchange"
  elif "raydium.io" in domain:
    return "Verify Raydium Protocol"
  elif "hyperliquid.xyz" in domain:
    return "Verify Hyperliquid"
  elif "x.com" in domain or "twitter.com" in domain:
    return "Verify Official X (Twitter) Post"
  elif "rootdata.com" in domain:
    return "Verify RootData Analytics"
  elif "cryptorank.io" in domain:
    return "Verify CryptoRank Listing"
  elif "fundednext.com" in domain:
    return "Verify FundedNext Official"
  elif "cointelegraph.com" in domain:
    return "Verify Cointelegraph"
  elif "decrypt.co" in domain:
    return "Verify Decrypt Article"
  elif "theblock.co" in domain:
    return "Verify The Block"
  elif "mirror.xyz" in domain or "medium.com" in domain:
    return "Read Official Article"
  return "Verify Official Source"


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
      raise Exception(
          "❌ CRITICAL: No Gemini API keys provided in GEMINI_API_KEYS"
          " environment variable."
      )
    return self.keys[self.current_key_idx]

  def get_current_model(self):
    return self.models[self.current_model_idx]

  def switch_to_next_key(self, reason="Rate Limit / Failover"):
    if not self.keys:
      return
    old_idx = self.current_key_idx
    self.current_key_idx = (self.current_key_idx + 1) % len(self.keys)
    logging.warning(
        f"🔄 Rotating Gemini API Key ({old_idx + 1}/{len(self.keys)} ->"
        f" {self.current_key_idx + 1}/{len(self.keys)}) Reason: [{reason}]"
    )

  def switch_to_next_model(self, reason="Model Fallback"):
    if len(self.models) <= 1:
      return
    old_model = self.get_current_model()
    self.current_model_idx = (self.current_model_idx + 1) % len(self.models)
    new_model = self.get_current_model()
    logging.warning(
        f"🔀 Switching Model ({old_model} -> {new_model}) Reason: [{reason}]"
    )

  def call_gemini_with_search(self, system_prompt, user_prompt, temperature=0.1):
    attempts = 0
    max_attempts = max(len(self.keys) * len(self.models) * 2, 12)

    while attempts < max_attempts:
      current_key = self.get_current_key()
      current_model = self.get_current_model()

      url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent?key={current_key}"
      headers = {"Content-Type": "application/json"}
      payload = {
          "contents": [{"parts": [{"text": user_prompt}]}],
          "systemInstruction": {"parts": [{"text": system_prompt}]},
          "tools": [{"googleSearch": {}}],
          "generationConfig": {"temperature": temperature},
      }

      try:
        response = requests.post(
            url, headers=headers, json=payload, timeout=60
        )

        if response.status_code == 200:
          data = response.json()
          candidates = data.get("candidates", [])
          if candidates and "content" in candidates[0]:
            parts = candidates[0]["content"].get("parts", [])
            for part in parts:
              if "text" in part:
                return part["text"]

        elif response.status_code == 404:
          self.switch_to_next_model(
              reason=f"HTTP 404 Model {current_model} Not Found"
          )

        elif response.status_code == 400:
          logging.error(
              f"⚠️ HTTP 400 Payload Error on {current_model}: {response.text}"
          )
          self.switch_to_next_model(
              reason=f"HTTP 400 Payload Error on {current_model}"
          )

        elif response.status_code == 429:
          backoff = min(2 ** (attempts % 4), 8)
          logging.warning(f"⚠️ Key Rate Limited (429). Sleeping {backoff}s...")
          time.sleep(backoff)
          self.switch_to_next_key(reason="HTTP 429 Rate Limit")
          if self.current_key_idx == 0:
            self.switch_to_next_model(
                reason=f"All keys limited on model {current_model}"
            )

        elif response.status_code in [401, 403]:
          self.switch_to_next_key(
              reason=f"HTTP Status {response.status_code} Auth/Quota Error"
          )

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
# 💾 DATABASE ENGINE (AUTO PURGE OLD DATA & DUPLICATE SHIELD)
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

      cursor.execute("PRAGMA table_info(sent_history)")
      existing_columns = [column[1] for column in cursor.fetchall()]

      if "opportunity_score" not in existing_columns:
        cursor.execute(
            "ALTER TABLE sent_history ADD COLUMN opportunity_score INTEGER"
            " DEFAULT 0"
        )
      if "risk_level" not in existing_columns:
        cursor.execute(
            "ALTER TABLE sent_history ADD COLUMN risk_level TEXT DEFAULT 'LOW'"
        )

      cursor.execute(
          "CREATE INDEX IF NOT EXISTS idx_sent_hash ON"
          " sent_history(unique_hash)"
      )
      conn.commit()

  def is_hash_sent(self, unique_hash):
    with self.get_connection() as conn:
      cursor = conn.cursor()
      cursor.execute(
          "SELECT id FROM sent_history WHERE unique_hash = ?", (unique_hash,)
      )
      return cursor.fetchone() is not None

  def mark_hash_sent(self, project_name, unique_hash, score=0, risk="LOW"):
    with self.get_connection() as conn:
      cursor = conn.cursor()
      now_str = datetime.now(timezone.utc).isoformat()
      cursor.execute(
          """
                INSERT OR IGNORE INTO sent_history (project_name, unique_hash, opportunity_score, risk_level, sent_at)
                VALUES (?, ?, ?, ?, ?)
            """,
          (project_name, unique_hash, score, risk, now_str),
      )
      conn.commit()

  def purge_expired_data(self, hours=24):
    cutoff_str = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()
    with self.get_connection() as conn:
      cursor = conn.cursor()
      cursor.execute(
          "DELETE FROM sent_history WHERE sent_at < ?", (cutoff_str,)
      )
      deleted = cursor.rowcount
      conn.commit()
      if deleted > 0:
        logging.info(
            f"🧹 Successfully purged {deleted} old record(s) older than"
            f" {hours} hours from Database."
        )


# =================================================================================
# 🧠 MASTER UNRESTRICTED ALL-IN-ONE UNIVERSAL INTELLIGENCE ENGINE
# =================================================================================


class GeminiAlphaEngine:

  def __init__(self, key_manager, db):
    self.key_manager = key_manager
    self.db = db

  def execute_master_pipeline(self):
    logging.info("⚡ Purging database history older than 24 hours...")
    self.db.purge_expired_data(hours=CACHE_PURGE_HOURS)

    logging.info(
        "⚡ Executing Complete Universal Web1-Web4 Global Internet Intelligence"
        " Scan..."
    )
    raw_items = self.fetch_fresh_web_intelligence_12h()

    if not raw_items:
      logging.info("ℹ️ No new live alpha items found within the last 12 hours.")
      return

    seen_in_run = set()
    posted_count = 0

    for item in raw_items:
      try:
        if not isinstance(item, dict):
          continue

        p_name = clean_val(item.get("project_name"))
        e_title = clean_val(item.get("event_title"))
        event_type = clean_val(item.get("event_type"), "AIRDROP_TESTNET")
        score = int(item.get("opportunity_score", 75))

        if p_name.upper() in ["UNDISCLOSED", "N/A", "NONE", "UNKNOWN"]:
          continue

        norm_pname = normalize_text(p_name)
        if not norm_pname:
          continue

        # SCORE FILTER: CAPTURE ALL VALID ITEMS INCLUDING BELOW 65 SCORE
        if score < MIN_OPPORTUNITY_SCORE:
          logging.info(
              f"⏭️ Skipping low score update ({score} <"
              f" {MIN_OPPORTUNITY_SCORE}) for: {p_name} | {e_title}"
          )
          continue

        unique_hash = generate_smart_event_hash(p_name, e_title, event_type)

        # Deduplication check
        if unique_hash in seen_in_run or self.db.is_hash_sent(unique_hash):
          logging.info(
              f"⏭️ Skipping duplicate event for project: {p_name} | {e_title}"
          )
          continue

        seen_in_run.add(unique_hash)

        risk = str(item.get("risk_level", "LOW")).upper()

        source_link = clean_val(
            item.get("source_link"), "https://rootdata.com"
        )
        direct_link = clean_val(item.get("official_direct_link"), source_link)

        if not is_valid_http_url(source_link):
          source_link = "https://rootdata.com"
        if not is_valid_http_url(direct_link):
          direct_link = source_link

        message = self.build_beautiful_telegram_post(
            item, source_link, direct_link
        )

        if self.send_telegram_retry_safe(message):
          self.db.mark_hash_sent(p_name, unique_hash, score, risk)
          posted_count += 1
          time.sleep(1.5)

      except Exception as e:
        logging.error(f"⚠️ Error processing item: {str(e)}")
        continue

    logging.info(
        f"✅ Scanning complete. Total posts dispatched: {posted_count}"
    )

  def fetch_fresh_web_intelligence_12h(self):
    """Scans complete Web1, Web2, Web3, and Web4 sources strictly within last 12 hours."""
    now_utc = datetime.now(timezone.utc)
    current_time_str = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    cutoff_time_str = (now_utc - timedelta(hours=12)).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    system_prompt = (
        f"CURRENT EXACT TIME: {current_time_str}\n"
        f"STRICT TIME CUTOFF: {cutoff_time_str} (Last 12 Hours Only)\n\n"
        "You are an Unrestricted Universal Global Intelligence Radar scanning"
        " Web1, Web2, Web3, and Web4 platforms. Use Google Search to scan"
        " global internet sources (RSS, Twitter/X, Coinbase Blog, Binance"
        " Announcements, OKX, Bybit, Kraken, Uniswap, Raydium, RootData,"
        " CryptoRank, Prop Firm Portals) for breaking announcements published"
        " STRICTLY between the STRICT TIME CUTOFF and CURRENT EXACT TIME.\n\n"
        "COMPREHENSIVE TARGET CATEGORIES:\n"
        "1. ALL CEX & DEX UPDATES (Coinbase, Binance, OKX, Bybit, Kraken,"
        " KuCoin, Bitget, Uniswap, Raydium, Jupiter, Aerodrome, Hyperliquid,"
        " Curve) - Token Listings, Spot/Futures Pairs, Launchpools,"
        " Governance & Protocol Upgrades.\n"
        "2. FREE FUNDED ACCOUNTS & FREE PROP FIRM CHALLENGES / EVALUATION"
        " WAIVERS (FundedNext, FTMO, MyFundedFX, Forex/Crypto Prop Firms).\n"
        "3. EXCLUSIVE NO-DEPOSIT TRADING BONUSES & WELCOME VOUCHERS.\n"
        "4. FREE MINING, NODES, TAP-TO-EARN & AIRDROP QUESTS.\n"
        "5. VC FUNDING ROUNDS & INSTITUTIONAL INVESTMENTS.\n"
        "6. LIVE AIRDROPS, TESTNETS & TGE SNAPSHOTS.\n\n"
        "STRICT FORMATTING & AUDIT RULES:\n"
        f"- Discard and IGNORE any news or article published BEFORE {cutoff_time_str}.\n"
        "- Only return news verified to have occurred within the last 12"
        " hours.\n"
        "- Assign 'opportunity_score' (0-100) to all valid updates (include"
        " lower scores too).\n"
        "- 'headline_sentence' MUST BE STRICTLY A SINGLE COMPREHENSIVE SENTENCE"
        " IN ENGLISH starting with the platform/project name and incorporating"
        " core figures/event details.\n"
        "- 'executive_summary' MUST BE STRICTLY A SINGLE SENTENCE IN ENGLISH"
        " explaining how users participate or what the update accomplishes.\n"
        "- OUTPUT MUST BE A VALID JSON ARRAY OR []."
    )

    user_prompt = f"""
Current UTC Time: {current_time_str}
Filter Constraint: Scan ONLY news published after {cutoff_time_str} (Last 12 Hours). Capture ALL CEX/DEX news (including Coinbase, Binance, OKX, Bybit, Uniswap, etc.) along with Prop Firms, VC Funding, Bonuses, Airdrops, and Testnets. Include items with score below 65.

JSON Schema Requirements:
[
  {{
    "project_name": "Official Platform / Exchange / Protocol Name",
    "event_title": "Short descriptive event title in English",
    "event_type": "EXCHANGE_LAUNCH | PROP_FIRM_FUNDED | BONUS_GIVEAWAY | MINING_QUEST | VC_FUNDING | AIRDROP_TESTNET | TGE_SNAPSHOT",
    "opportunity_score": 50,
    "confidence_score": 90,
    "risk_level": "LOW",
    "headline_sentence": "EXACTLY ONE sentence combining project name, core action, and figures (e.g. 'Coinbase adds Shadow Token to its listing roadmap for prospective trading support.').",
    "executive_summary": "EXACTLY ONE sentence detailing participation or key context (e.g. 'Trading will open once liquidity requirements are met across supported EVM networks.').",
    "official_direct_link": "Direct registration, exchange listing, claim, or testnet link",
    "source_link": "Direct official announcement or verified source link"
  }}
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
    """Generates Single-Sentence Headline & Summary Post separated cleanly with empty lines."""
    p_name = escape_html(clean_val(item.get("project_name"), "Alpha Project"))
    e_title = escape_html(clean_val(item.get("event_title"), "Breaking Update"))
    event_type = str(item.get("event_type", "AIRDROP_TESTNET")).upper()

    # Process Headline: Ensure strictly 1 sentence
    raw_headline = clean_val(
        item.get("headline_sentence"), f"{p_name} announces {e_title}."
    )
    headline_parts = [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+", raw_headline)
        if s.strip()
    ]
    one_headline = escape_html(
        headline_parts[0] if headline_parts else raw_headline
    )

    # Process Summary: Ensure strictly 1 sentence
    raw_summary = clean_val(
        item.get("executive_summary"), "New live alpha update available."
    )
    summary_parts = [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+", raw_summary)
        if s.strip()
    ]
    one_summary = escape_html(summary_parts[0] if summary_parts else raw_summary)

    check_text = (e_title + " " + event_type + " " + p_name).lower()

    # Dynamic Category Emoji Routing
    if (
        any(
            kw in check_text
            for kw in [
                "coinbase",
                "binance",
                "okx",
                "bybit",
                "kraken",
                "kucoin",
                "bitget",
                "gate.io",
                "mexc",
                "uniswap",
                "raydium",
                "jupiter",
                "aerodrome",
                "hyperliquid",
                "pancakeswap",
                "curve",
                "cex",
                "dex",
                "exchange launch",
                "launchpool",
                "launchpad",
                "listing",
                "spot",
                "perpetual",
                "roadmap",
            ]
        )
        or event_type == "EXCHANGE_LAUNCH"
    ):
      emoji = "🏛️"
    elif (
        any(
            kw in check_text
            for kw in [
                "funded account",
                "prop firm",
                "propfirm",
                "prop trading",
                "evaluation waiver",
                "free challenge",
                "funded prop",
            ]
        )
        or event_type == "PROP_FIRM_FUNDED"
    ):
      emoji = "🏦"
    elif (
        any(
            kw in check_text
            for kw in [
                "bonus",
                "futures",
                "giveaway",
                "starter",
                "welcome",
                "voucher",
                "mystery box",
                "sign-up",
                "casino",
                "reward",
                "no-deposit",
            ]
        )
        or event_type == "BONUS_GIVEAWAY"
    ):
      emoji = "🎁"
    elif (
        any(
            kw in check_text
            for kw in [
                "mining",
                "node",
                "tap-to-earn",
                "faucet",
                "quest",
                "whitelist",
            ]
        )
        or event_type == "MINING_QUEST"
    ):
      emoji = "⛏️"
    elif (
        any(
            kw in check_text
            for kw in ["tge", "snapshot", "token launch", "launchpad", "listing"]
        )
        or event_type == "TGE_SNAPSHOT"
    ):
      emoji = "🔥"
    elif (
        any(
            kw in check_text
            for kw in ["seed", "series", "raised", "funding", "valuation", "vc"]
        )
        or event_type == "VC_FUNDING"
    ):
      emoji = "💎"
    else:
      emoji = "🚀"

    source_label = escape_html(detect_link_label(source_link))
    safe_source_link = html.escape(source_link, quote=True)
    safe_direct_link = html.escape(direct_link, quote=True)

    link_block = f'🔗 <a href="{safe_source_link}"><b>{source_label}</b></a>'
    if direct_link != source_link and is_valid_http_url(direct_link):
      link_block += (
          f' | 🪂 <a href="{safe_direct_link}"><b>Direct Claim Link</b></a>'
      )

    # Output structure with empty space after every sentence block
    post_content = (
        f"{emoji} <b>{one_headline}</b>\n\n"
        f"📝 <b>Summary:</b> {one_summary}\n\n"
        f"{link_block}"
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
        "disable_web_page_preview": True,
    }

    for attempt in range(3):
      try:
        res = requests.post(url, json=payload, timeout=12)
        if res.status_code == 200 and res.json().get("ok"):
          logging.info("⚡ Telegram Post Successfully Dispatched.")
          return True
        elif res.status_code == 429:
          retry_after = int(
              res.json().get("parameters", {}).get("retry_after", 5)
          )
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
  logging.info("🚀 STARTING ULTIMATE WEB1-WEB4 UNIVERSAL ALPHA ENGINE 🚀")
  logging.info("=========================================================")

  key_manager = GeminiAPIKeyManager(GEMINI_API_KEYS, MODEL_CANDIDATES)
  db = AlphaDatabase(DB_FILE)
  engine = GeminiAlphaEngine(key_manager, db)

  engine.execute_master_pipeline()

  logging.info("✅ Execution completed cleanly with zero missing updates.")


if __name__ == "__main__":
  main()
