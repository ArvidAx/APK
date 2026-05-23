import streamlit as st
import pandas as pd
import httpx
import json
import os
import re
import logging
import datetime
from typing import Dict, List

# Set up logging to stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("APK_App")

# Constants
API_URL = "https://susbolaget.emrik.org/v1/products"
FALLBACK_FILE = "local_products_fallback.json"

# Configure Streamlit page layout and theme attributes
st.set_page_config(
    page_title="Systembolaget APK-Analysator",
    page_icon="🍺",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Premium clean CSS
st.markdown("""
<style>
    .stApp {
        background-color: #0b0f19;
        color: #f1f5f9;
    }
    h1, h2, h3 {
        font-family: 'Inter', sans-serif !important;
        font-weight: 600 !important;
    }
    .kpi-card {
        flex: 1;
        background: #111827;
        border-radius: 8px;
        padding: 1.25rem;
        border: 1px solid #1f2937;
        border-left: 4px solid #3b82f6;
        transition: border-color 0.2s ease;
        margin-bottom: 0.5rem;
    }
    .kpi-card:hover { border-color: #4b5563; }
    .kpi-card.gold  { border-left-color: #d97706; }
    .kpi-card.silver{ border-left-color: #6b7280; }
    .kpi-card.bronze{ border-left-color: #b45309; }
    .kpi-title {
        font-size: 0.75rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: #9ca3af;
        margin-bottom: 0.25rem;
    }
    .kpi-name {
        font-size: 1.1rem;
        font-weight: 600;
        color: #ffffff;
        margin-bottom: 0.5rem;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .kpi-value {
        font-size: 1.8rem;
        font-weight: 700;
        color: #10b981;
        line-height: 1;
    }
    .kpi-value-unit {
        font-size: 0.85rem;
        font-weight: 500;
        color: #9ca3af;
    }
    .kpi-meta {
        margin-top: 0.5rem;
        font-size: 0.8rem;
        color: #9ca3af;
        display: flex;
        gap: 0.5rem;
        flex-wrap: wrap;
    }
    .kpi-badge {
        background-color: #1f2937;
        padding: 0.1rem 0.4rem;
        border-radius: 4px;
        font-size: 0.75rem;
        border: 1px solid #374151;
    }
</style>
""", unsafe_allow_html=True)


def is_order_item(row: pd.Series) -> bool:
    """Checks if a product is a beställningsvara based on assortment properties."""
    assort = str(row.get("assortment") or "").strip().upper()
    assort_text = str(row.get("assortmentText") or "").strip().lower()
    return (assort == "BS") or ("beställningssortiment" in assort_text)


def construct_name(row: pd.Series, name_col: str, add_name_col: str) -> str:
    """Constructs the clean display name from Name + AdditionalName."""
    n = row.get(name_col)
    an = row.get(add_name_col)
    n_str = "" if pd.isna(n) or n is None else str(n).strip()
    an_str = "" if pd.isna(an) or an is None else str(an).strip()
    if an_str and an_str.lower() != "nan":
        return f"{n_str} ({an_str})"
    return n_str


def get_pant_sek(row: pd.Series) -> float:
    """
    Calculates the Swedish deposit (pant) in SEK.
    - Burk (metal can): 2.00 SEK
    - PET-flaska / Plastflaska: 2.00 SEK
    - Returglas: 0.60 SEK (<=33cl) or 0.90 SEK (>33cl)
    """
    packaging = str(row.get("bottleText") or "").strip().lower()
    if "burk" in packaging or "pet" in packaging or "plast" in packaging:
        return 2.00
    fee = row.get("recycleFee")
    if pd.notna(fee) and fee is not None:
        try:
            fee_val = float(fee)
            if fee_val == 1.0:
                return 1.00
            elif fee_val == 2.0:
                return 2.00
            elif fee_val == 3.0:
                vol = float(row.get("volume") or 0)
                return 0.60 if vol <= 330 else 0.90
            elif fee_val > 0:
                return fee_val / 100.0 if fee_val > 10.0 else fee_val
        except ValueError:
            pass
    if "returglas" in packaging:
        vol = float(row.get("volume") or 0)
        return 0.60 if vol <= 330 else 0.90
    return 0.00


def get_systembolaget_url(row: pd.Series) -> str:
    """Constructs the official Systembolaget product page URL."""
    prod_num = str(row.get("productNumber") or "").strip()
    if not prod_num:
        return "https://www.systembolaget.se/"
    cat = str(row.get("categoryLevel1") or "").strip().lower()
    cat_map = {
        "öl": "ol", "sprit": "sprit", "vin": "vin",
        "cider & blanddrycker": "cider-och-blanddrycker",
        "alkoholfritt": "alkoholfritt", "presenter": "presenter"
    }
    cat_slug = cat_map.get(cat, cat)
    cat_slug = cat_slug.replace("ö", "o").replace("ä", "a").replace("å", "a").replace("&", "och").replace(" ", "-")
    name = str(row.get("Name") or "").strip().lower()
    name_slug = name.replace("ö", "o").replace("ä", "a").replace("å", "a")
    name_slug = re.sub(r'[^a-z0-9\-]', '-', name_slug)
    name_slug = re.sub(r'-+', '-', name_slug).strip('-')
    return f"https://www.systembolaget.se/produkt/{cat_slug}/{name_slug}-{prod_num}/"


def query_groq_occasion(user_prompt: str) -> dict:
    """
    Queries the Groq API using Llama 3.1 (llama-3.1-8b-instant) to get structured recommendations
    for the user's occasion.
    """
    api_key = st.secrets.get("GROQ_API_KEY", "") or os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        logger.error("Groq API key not found in secrets or environment.")
        return {
            "error": "Groq API-nyckel saknas. Lägg till GROQ_API_KEY i .streamlit/secrets.toml för att använda AI-sök."
        }
        
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    system_prompt = """Du är en expert-sommelier för Systembolagets sortiment. Din uppgift är att analysera användarens tillfälle och ge förslag på passande drycker.

ABSOLUTA REGLER:
1. Du MÅSTE sätta 'max_price' till null såvida inte användaren explicit nämner pengar, budget eller ett pris i sin inmatning. Hitta ALDRIG på ett maxpris själv!
2. Du MÅSTE sätta 'min_alc' och 'max_alc' till null såvida inte användaren efterfrågar t.ex. 'alkoholfritt' eller en specifik styrka. Hitta ALDRIG på en alkoholhalt själv!
3. Om tillfället rör snaps, nubbe, sprit eller liknande, är kategorin alltid 'Sprit' och alkoholhalten ska vara null!

Du MÅSTE svara med ett giltigt JSON-objekt med exakt denna struktur:
{
  "explanation": "En kort, inspirerande förklaring på svenska om varför rekommendationerna passar tillfället (max 2 meningar).",
  "categories": ["kategori1", "kategori2"],
  "subcategories": ["underkategori1", "underkategori2"],
  "keywords": ["sökord1", "sökord2"],
  "min_alc": null,
  "max_alc": null,
  "max_price": null,
  "min_apk": null
}

Viktiga regler för fälten:
- 'categories' MÅSTE vara en lista av noll eller flera av dessa exakta svenska kategorinamn: "Öl", "Vin", "Sprit", "Cider & blanddrycker", "Alkoholfritt", "Presenter". Om alla kategorier passar, returnera en tom lista [].
- 'subcategories' MÅSTE vara en lista av noll eller flera passande svenska underkategorier (t.ex. "Akvavit & Kryddat brännvin", "Ljus lager", "IPA", "Cider", "Rött vin", "Champagne", "Maltwhisky"). Föreslå gärna flera passande underkategorier för att täcka in rätt sortiment (t.ex. för snaps/nubbe kan du föreslå både "Akvavit & Kryddat brännvin" och "Kryddat brännvin"). Om alla underkategorier passar, returnera en tom lista [].
- 'keywords' MÅSTE vara en lista med 2 till 5 korta, relevanta sökord på svenska i singular och gemener (t.ex. "ipa", "lager", "fruktigt", "kryddigt", "snaps", "champagne", "bordeaux", "friskt", "sommar") för att söka i produktnamn, underkategori eller producent.
- 'max_price': Sätt alltid till null om inte budget/pris nämns.
- 'min_alc' och 'max_alc': Sätt alltid till null om inte styrka/alkoholhalt nämns.
- 'min_apk': Sätt alltid till null om inte billigaste/budget nämns.
"""

    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Tillfälle: {user_prompt}"}
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"}
    }
    
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            res_json = response.json()
            content = res_json["choices"][0]["message"]["content"]
            result = json.loads(content)
            return result
    except httpx.HTTPStatusError as he:
        logger.error(f"Groq API error: {he.response.status_code} - {he.response.text}")
        return {"error": f"Groq API returnerade felkod {he.response.status_code}."}
    except Exception as e:
        logger.error(f"Failed to query Groq: {e}")
        return {"error": f"Det gick inte att kontakta AI-tjänsten: {str(e)}"}


@st.cache_data(ttl=86400)
def load_data() -> pd.DataFrame:
    """
    Loads Systembolaget assortment data.
    First checks if local_products_fallback.json is fresh (<24h).
    If so, loads from disk instantly. Otherwise fetches from API and saves locally.
    """
    data = None
    loaded_from_cache = False
    fetched_from_api = False

    # Try fresh local cache first
    if os.path.exists(FALLBACK_FILE):
        try:
            mtime = os.path.getmtime(FALLBACK_FILE)
            if datetime.datetime.now().timestamp() - mtime < 86400:
                logger.info("Local cache is fresh. Loading from disk...")
                with open(FALLBACK_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                loaded_from_cache = True
                logger.info("Loaded from local disk cache.")
        except Exception as fe:
            logger.error(f"Failed to read local cache: {fe}")

    # Fetch from API if no fresh local cache
    if not loaded_from_cache:
        try:
            logger.info(f"Fetching from API: {API_URL}")
            with httpx.Client(timeout=30.0) as client:
                response = client.get(API_URL)
                response.raise_for_status()
                data = response.json()
                fetched_from_api = True
                logger.info("Fetched from API successfully.")
        except Exception as e:
            logger.error(f"API fetch failed: {e}")
            if os.path.exists(FALLBACK_FILE):
                logger.info("API unreachable — using older local cache.")
                try:
                    with open(FALLBACK_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception as fe:
                    raise RuntimeError(f"API failed and local cache is corrupted: {fe}")
            else:
                raise RuntimeError(f"API failed and no local fallback exists.")

    if not data:
        raise ValueError("Assortment payload is empty.")

    df = pd.DataFrame(data)
    if df.empty:
        raise ValueError("Assortment DataFrame is empty.")

    # Ensure vital columns exist
    for col in ["price", "volume", "alcoholPercentage", "categoryLevel1",
                "categoryLevel2", "assortment", "assortmentText", "producerName"]:
        if col not in df.columns:
            df[col] = None

    # Numeric conversions
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["alcoholPercentage"] = pd.to_numeric(df["alcoholPercentage"], errors="coerce")

    # Drop invalid rows
    initial_rows = len(df)
    df = df.dropna(subset=["price", "volume", "alcoholPercentage"])
    df = df[(df["price"] > 0) & (df["volume"] > 0) & (df["alcoholPercentage"] > 0)]
    logger.info(f"Cleaned: {initial_rows} → {len(df)} rows.")

    # Build display name
    name_col = "productNameBold" if "productNameBold" in df.columns else "name"
    add_name_col = "productNameThin" if "productNameThin" in df.columns else "additionalName"
    df["Name"] = df.apply(lambda r: construct_name(r, name_col, add_name_col), axis=1)

    # Categories
    df["Main Category"] = df["categoryLevel1"].fillna("Övrigt").astype(str).str.strip()
    df["Subcategory"] = df["categoryLevel2"].fillna("Övrigt").astype(str).str.strip()

    # Assortment flags
    df["IsOrderAssortment"] = df.apply(is_order_item, axis=1)
    df["Order Assortment"] = df["IsOrderAssortment"].map({True: "Ja", False: "Nej"})
    df["Producer"] = df["producerName"].fillna("Okänd").astype(str).str.strip()

    # Pant & URL
    df["Pant"] = df.apply(get_pant_sek, axis=1)
    df["SystembolagetURL"] = df.apply(get_systembolaget_url, axis=1)

    # APK calculation
    df["APK"] = (df["volume"] * (df["alcoholPercentage"] / 100.0)) / df["price"]
    df["APK"] = df["APK"].round(4)

    # Sort & deduplicate
    df = df.sort_values(by="APK", ascending=False).reset_index(drop=True)
    initial_dedup = len(df)
    df = df.drop_duplicates(subset=["Name", "volume", "alcoholPercentage"], keep="first").reset_index(drop=True)
    logger.info(f"Dedup: {initial_dedup} → {len(df)} rows.")

    df["Rank"] = df.index + 1

    # Persist to local cache if freshly fetched
    if fetched_from_api:
        try:
            with open(FALLBACK_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info("Updated local fallback cache.")
        except Exception as se:
            logger.warning(f"Failed to write local cache: {se}")

    return df


# ─── Main execution ──────────────────────────────────────────────────────────
df = None
try:
    df = load_data()
except Exception as exc:
    logger.error(f"Fatal: load_data() failed: {exc}")
    st.error("Systemfel: Produktdata kunde inte hämtas")
    st.markdown(
        f"Applikationen misslyckades med att hämta Systembolagets data. "
        f"Kontrollera din internetanslutning eller försök igen.\n\n"
        f"**Felkod:** `{type(exc).__name__}`"
    )
    st.stop()

if df is None:
    st.stop()

# Prepare filter bounds and options
categories = sorted(df["Main Category"].unique().tolist())
default_cats = [c for c in ["Öl", "Sprit"] if c in categories]

min_alc_val = float(df["alcoholPercentage"].min())
max_alc_val = float(df["alcoholPercentage"].max())

min_price_val = int(df["price"].min())
max_price_val = int(df["price"].max())
options_under_1000 = list(range(min_price_val, min(1000, max_price_val) + 1, 5))
if min_price_val not in options_under_1000:
    options_under_1000.insert(0, min_price_val)
if max_price_val > 1000:
    options_above_1000 = list(range(1000, min(5000, max_price_val) + 1, 50))
    if max_price_val > 5000:
        options_above_1000 += list(range(5000, max_price_val + 1, 250))
    if max_price_val not in options_above_1000:
        options_above_1000.append(max_price_val)
    price_options = sorted(set(options_under_1000 + options_above_1000))
else:
    price_options = sorted(set(options_under_1000))

default_high = min(500, price_options[-1]) if price_options[-1] > 500 else price_options[-1]
if default_high not in price_options:
    default_high = min(price_options, key=lambda x: abs(x - default_high))

# Volume bounds definition
min_vol_val = int(df["volume"].min())
max_vol_val = int(df["volume"].max())

# Initialize session state variables for AI Occasion Finder
if "ai_occasion" not in st.session_state:
    st.session_state.ai_occasion = None
if "ai_filters" not in st.session_state:
    st.session_state.ai_filters = None
if "ai_error" not in st.session_state:
    st.session_state.ai_error = None

# Initialize session state for sidebar filter widgets to allow programmatic override
if "sb_categories" not in st.session_state:
    st.session_state.sb_categories = default_cats
if "sb_alc" not in st.session_state:
    st.session_state.sb_alc = (min_alc_val, max_alc_val)
if "sb_price" not in st.session_state:
    st.session_state.sb_price = (price_options[0], default_high)
if "sb_vol_slider" not in st.session_state:
    st.session_state.sb_vol_slider = (min_vol_val, max_vol_val)
if "sb_vol_min_input" not in st.session_state:
    st.session_state.sb_vol_min_input = min_vol_val
if "sb_vol_max_input" not in st.session_state:
    st.session_state.sb_vol_max_input = max_vol_val

# Helper function to reset AI filters and restore defaults
def reset_ai_search():
    st.session_state.ai_occasion = None
    st.session_state.ai_filters = None
    st.session_state.ai_error = None
    st.session_state.sb_categories = default_cats
    st.session_state.sb_alc = (min_alc_val, max_alc_val)
    st.session_state.sb_price = (price_options[0], default_high)
    st.session_state.sb_vol_slider = (min_vol_val, max_vol_val)
    st.session_state.sb_vol_min_input = min_vol_val
    st.session_state.sb_vol_max_input = max_vol_val

# Callback functions to synchronize volume slider and number inputs bidirectionally
def sync_vol_from_slider():
    st.session_state.sb_vol_min_input = st.session_state.sb_vol_slider[0]
    st.session_state.sb_vol_max_input = st.session_state.sb_vol_slider[1]

def sync_vol_from_inputs():
    mn = st.session_state.sb_vol_min_input
    mx = st.session_state.sb_vol_max_input
    if mn > mx:
        mn, mx = mx, mn
        st.session_state.sb_vol_min_input = mn
        st.session_state.sb_vol_max_input = mx
    st.session_state.sb_vol_slider = (mn, mx)


# Callback function for AI Occasion Finder
def run_ai_search():
    user_input = st.session_state.ai_input_val.strip() if "ai_input_val" in st.session_state else ""
    if not user_input:
        return
        
    ai_result = query_groq_occasion(user_input)
    
    if "error" in ai_result:
        st.session_state.ai_error = ai_result["error"]
        st.session_state.ai_filters = None
        st.session_state.ai_occasion = None
    else:
        # Heuristic overrides to defend against LLM hallucination of price or alcohol limits
        price_indicators = ["kr", "budget", "billig", "pris", "dyr", "lyx", "studentfest", "fattig", "pengar", "kostar", "snål", "spara"]
        has_price_request = any(ind in user_input.lower() for ind in price_indicators)
        
        alc_indicators = ["%", "procent", "alkohol", "alkoholfri", "svag", "stark", "lätt", "nykter", "supa", "fylla", "styrka", "promille"]
        has_alc_request = any(ind in user_input.lower() for ind in alc_indicators)
        
        # Snaps/sprit indicator checks
        sprit_indicators = ["snaps", "nubbe", "vodka", "gin", "rom", "whiskey", "whisky", "tequila", "sprit", "likör", "snapsar", "nubbar"]
        is_sprit_request = any(ind in user_input.lower() for ind in sprit_indicators)
        
        if is_sprit_request:
            # Snap/Nubbe overrides: ensure Sprit category and clear any accidental alcohol limits
            ai_result["categories"] = ["Sprit"]
            ai_result["min_alc"] = None
            ai_result["max_alc"] = None
            
            # Snap/Nubbe specific subcategory override
            snaps_indicators = ["snaps", "nubbe", "snapsar", "nubbar"]
            if any(ind in user_input.lower() for ind in snaps_indicators):
                ai_result["subcategories"] = ["Akvavit & Kryddat brännvin", "Kryddat brännvin"]
        else:
            if not has_alc_request:
                ai_result["min_alc"] = None
                ai_result["max_alc"] = None
                
        if not has_price_request:
            ai_result["max_price"] = None
            ai_result["min_apk"] = None
            
        st.session_state.ai_filters = ai_result
        st.session_state.ai_occasion = user_input
        st.session_state.ai_error = None
        
        # Apply filters programmatically to sidebar widgets
        ai_cats = ai_result.get("categories", [])
        valid_cats = [c for c in ai_cats if c in categories]
        if valid_cats:
            st.session_state.sb_categories = valid_cats
        else:
            st.session_state.sb_categories = categories
            
        min_a = ai_result.get("min_alc")
        max_a = ai_result.get("max_alc")
        min_val = float(min_alc_val)
        max_val = float(max_alc_val)
        min_a_f = max(min_val, float(min_a)) if min_a is not None else min_val
        max_a_f = min(max_val, float(max_a)) if max_a is not None else max_val
        if min_a_f >= max_a_f:
            max_a_f = min(max_val, min_a_f + 0.5)
            if min_a_f >= max_a_f:
                min_a_f = max(min_val, max_a_f - 0.5)
        st.session_state.sb_alc = (min_a_f, max_a_f)
        
        max_p = ai_result.get("max_price")
        if max_p is not None:
            max_p_float = float(max_p)
            closest_max = min(price_options, key=lambda x: abs(x - max_p_float))
            if closest_max <= price_options[0]:
                closest_max = price_options[1]
            st.session_state.sb_price = (price_options[0], closest_max)
        else:
            # If no max price is requested, set the slider upper limit to the absolute maximum price option
            st.session_state.sb_price = (price_options[0], price_options[-1])

# ─── Header ──────────────────────────────────────────────────────────────────
st.title("Systembolaget APK-Analysator")
today_str = datetime.date.today().strftime("%Y-%m-%d")
st.markdown(
    f"Sök, filtrera och analysera prisvärdheten på Systembolagets sortiment baserat på "
    f"**APK (Alkohol Per Krona)**. Data uppdateras dagligen. Senaste körning: {today_str}"
)

# ─── Sidebar ─────────────────────────────────────────────────────────────────
st.sidebar.markdown("### Systemstatus")
st.sidebar.info(f"Totalt sortiment: {len(df):,} produkter laddade.")

st.sidebar.markdown("### Filter")

include_pant = st.sidebar.toggle(
    "Inkludera pant i priset",
    value=False,
    help="Burkar, PET-flaskor och returglas har pant i Sverige. Aktivera för att räkna APK på totalpriset."
)

selected_categories = st.sidebar.multiselect(
    "Välj kategorier",
    options=categories,
    default=st.session_state.sb_categories,
    key="sb_categories"
)

show_order_items = st.sidebar.checkbox(
    "Visa beställningsvaror (BS)",
    value=False,
)

selected_alc = st.sidebar.slider(
    "Alkoholhalt (%)",
    min_value=min_alc_val,
    max_value=max_alc_val,
    value=st.session_state.sb_alc,
    step=0.5,
    format="%.1f%%",
    key="sb_alc"
)

selected_price_low, selected_price_high = st.sidebar.select_slider(
    "Pris (SEK)",
    options=price_options,
    value=st.session_state.sb_price,
    format_func=lambda val: f"{val} kr",
    key="sb_price"
)

# Volume Range controls with bidirectional synchronization between Slider and Number Inputs
st.sidebar.markdown("### Volym (ml)")
selected_vol = st.sidebar.slider(
    "Volymintervall (ml)",
    min_value=min_vol_val,
    max_value=max_vol_val,
    value=st.session_state.sb_vol_slider,
    step=10,
    format="%d ml",
    key="sb_vol_slider",
    on_change=sync_vol_from_slider
)

col_vol1, col_vol2 = st.sidebar.columns(2)
with col_vol1:
    st.number_input(
        "Min volym (ml)",
        min_value=min_vol_val,
        max_value=max_vol_val,
        value=st.session_state.sb_vol_min_input,
        step=50,
        key="sb_vol_min_input",
        on_change=sync_vol_from_inputs
    )
with col_vol2:
    st.number_input(
        "Max volym (ml)",
        min_value=min_vol_val,
        max_value=max_vol_val,
        value=st.session_state.sb_vol_max_input,
        step=50,
        key="sb_vol_max_input",
        on_change=sync_vol_from_inputs
    )

search_query = st.sidebar.text_input(
    "Sök produkt eller producent",
    placeholder="T.ex. Falcon, Absolut, Bordeaux...",
)

# ─── Apply filters ────────────────────────────────────────────────────────────
filtered_df = df.copy()

# Apply AI keyword and deep filters first if active
if st.session_state.ai_filters:
    ai_f = st.session_state.ai_filters
    
    # Filter by AI keywords in name, subcategory, or producer (OR search)
    ai_keywords = ai_f.get("keywords", [])
    if ai_keywords:
        keyword_mask = pd.Series(False, index=filtered_df.index)
        has_valid_kw = False
        for kw in ai_keywords:
            kw_clean = str(kw).strip().lower()
            if not kw_clean:
                continue
            has_valid_kw = True
            name_match = filtered_df["Name"].str.lower().str.contains(kw_clean, na=False)
            sub_match = filtered_df["Subcategory"].str.lower().str.contains(kw_clean, na=False)
            prod_match = filtered_df["Producer"].str.lower().str.contains(kw_clean, na=False)
            keyword_mask = keyword_mask | name_match | sub_match | prod_match
        if has_valid_kw:
            filtered_df = filtered_df[keyword_mask]
            
    # Filter by AI subcategories if specified (OR check for case-insensitive substrings)
    ai_subcategories = ai_f.get("subcategories", [])
    if ai_subcategories:
        sub_mask = pd.Series(False, index=filtered_df.index)
        has_valid_sub = False
        for sub_term in ai_subcategories:
            sub_term_clean = str(sub_term).strip().lower()
            if not sub_term_clean:
                continue
            has_valid_sub = True
            match_sub = filtered_df["Subcategory"].str.lower().str.contains(sub_term_clean, na=False)
            sub_mask = sub_mask | match_sub
            
        if has_valid_sub:
            filtered_df = filtered_df[sub_mask]
            
    # Filter by AI min APK if present
    min_apk = ai_f.get("min_apk")
    if min_apk is not None:
        try:
            filtered_df = filtered_df[filtered_df["APK"] >= float(min_apk)]
        except ValueError:
            pass

filtered_df["Display Price"] = filtered_df["price"] + filtered_df["Pant"] if include_pant else filtered_df["price"]
filtered_df["APK"] = (filtered_df["volume"] * (filtered_df["alcoholPercentage"] / 100.0)) / filtered_df["Display Price"]
filtered_df["APK"] = filtered_df["APK"].round(4)

if selected_categories:
    filtered_df = filtered_df[filtered_df["Main Category"].isin(selected_categories)]

if not show_order_items:
    filtered_df = filtered_df[~filtered_df["IsOrderAssortment"]]

filtered_df = filtered_df[
    (filtered_df["alcoholPercentage"] >= selected_alc[0]) &
    (filtered_df["alcoholPercentage"] <= selected_alc[1])
]

filtered_df = filtered_df[
    (filtered_df["price"] >= selected_price_low) &
    (filtered_df["price"] <= selected_price_high)
]

filtered_df = filtered_df[
    (filtered_df["volume"] >= selected_vol[0]) &
    (filtered_df["volume"] <= selected_vol[1])
]

if search_query:
    q = search_query.strip().lower()
    filtered_df = filtered_df[
        filtered_df["Name"].str.lower().str.contains(q, na=False) |
        filtered_df["Producer"].str.lower().str.contains(q, na=False)
    ]

filtered_df = filtered_df.sort_values(by="APK", ascending=False).reset_index(drop=True)
filtered_df["Rank"] = filtered_df.index + 1

# ─── AI Occasion Finder UI ────────────────────────────────────────────────────
st.markdown("---")
with st.container():
    st.markdown("### 🤖 Sök dryck med AI")
    st.markdown(
        "Beskriv vad du ska nyttja drycken till (t.ex. *grillkväll med ryggbiff*, "
        "*sommarpicknick i parken*, *kräftskiva* eller *studentfest på budget*). "
        "En AI ger förslag på kategorier, sökord och alkoholhalt anpassade för tillfället, "
        "vilka sedan sorteras efter APK."
    )
    
    col_input, col_btn = st.columns([4, 1])
    with col_input:
        ai_input = st.text_input(
            "Beskriv tillfället:",
            placeholder="T.ex. grillfest, kräftskiva, fira födelsedag, budgetförfest...",
            label_visibility="collapsed",
            key="ai_input_val"
        )
    with col_btn:
        # Use on_click callback to execute before widget instantiation to avoid StreamlitAPIException
        st.button("Hitta med AI ⚡", on_click=run_ai_search, use_container_width=True)

    # Display errors if any
    if st.session_state.ai_error:
        st.error(st.session_state.ai_error)
        if st.button("Rensa felmeddelande"):
            st.session_state.ai_error = None
            st.rerun()

    # Display active AI search details and explanation
    if st.session_state.ai_filters:
        ai_f = st.session_state.ai_filters
        
        # Build badges HTML dynamically to prevent literal HTML rendering bugs in Markdown parser
        badges = []
        
        # Categories badge
        cats = ai_f.get('categories', [])
        cats_str = ", ".join(cats) if cats else 'Alla'
        badges.append(f'<span style="background-color: #064e3b; color: #a7f3d0; border: 1px solid #047857; padding: 0.15rem 0.5rem; border-radius: 4px; font-size: 0.8rem;">Kategorier: {cats_str}</span>')
        
        # Subcategories badge
        subcats = ai_f.get('subcategories', [])
        if subcats:
            subcats_str = ", ".join(subcats)
            badges.append(f'<span style="background-color: #0d9488; color: #ccfbf1; border: 1px solid #0f766e; padding: 0.15rem 0.5rem; border-radius: 4px; font-size: 0.8rem;">Underkategorier: {subcats_str}</span>')
            
        # Keywords badges
        keywords = ai_f.get('keywords', [])
        for kw in keywords:
            kw_clean = str(kw).strip()
            if kw_clean:
                badges.append(f'<span style="background-color: #1e3a8a; color: #dbeafe; border: 1px solid #1d4ed8; padding: 0.15rem 0.5rem; border-radius: 4px; font-size: 0.8rem;">Nyckelord: {kw_clean}</span>')
                
        # Alcohol range badge
        min_a = ai_f.get("min_alc")
        max_a = ai_f.get("max_alc")
        if min_a is not None or max_a is not None:
            min_a_val = min_a if min_a is not None else 0
            max_a_val = max_a if max_a is not None else 100
            badges.append(f'<span style="background-color: #701a75; color: #fdf4ff; border: 1px solid #a21caf; padding: 0.15rem 0.5rem; border-radius: 4px; font-size: 0.8rem;">Alkoholhalt: {min_a_val}% - {max_a_val}%</span>')
            
        # Max price badge
        max_p = ai_f.get("max_price")
        if max_p is not None:
            badges.append(f'<span style="background-color: #7c2d12; color: #ffedd5; border: 1px solid #c2410c; padding: 0.15rem 0.5rem; border-radius: 4px; font-size: 0.8rem;">Maxpris: {max_p} kr</span>')
            
        badges_html = "\n                ".join(badges)
        
        st.markdown(f"""
        <div style="background-color: #111827; border: 1px solid #1f2937; border-left: 4px solid #10b981; border-radius: 8px; padding: 1rem; margin-top: 1rem; margin-bottom: 1rem;">
            <div style="font-size: 0.8rem; font-weight: 600; color: #10b981; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.25rem;">🤖 AI-Sommelier Rekommendation</div>
            <div style="font-size: 1.1rem; font-weight: 600; color: #ffffff; margin-bottom: 0.5rem;">Tillfälle: "{st.session_state.ai_occasion}"</div>
            <div style="font-size: 0.95rem; color: #e2e8f0; line-height: 1.5; margin-bottom: 0.75rem;">{ai_f.get('explanation', '')}</div>
            <div style="display: flex; gap: 0.5rem; flex-wrap: wrap;">
                {badges_html}
            </div>
        </div>
        """, unsafe_allow_html=True)
        
        # Use on_click callback to clear state cleanly
        st.button("Rensa AI-sökning ❌", key="clear_ai", on_click=reset_ai_search)

st.markdown("---")

# ─── Top 3 KPI cards ─────────────────────────────────────────────────────────
st.markdown("### Mest prisvärda produkter")
if not filtered_df.empty:
    top_3 = filtered_df.head(3)
    kpi_cols = st.columns(3)
    medals = ["Rank 1", "Rank 2", "Rank 3"]
    classes = ["gold", "silver", "bronze"]

    for idx in range(3):
        with kpi_cols[idx]:
            if idx < len(top_3):
                row = top_3.iloc[idx]
                p_pant = row["Pant"]
                p_display = row["Display Price"]
                if p_pant > 0 and include_pant:
                    price_label = f"{p_display:.2f} kr (inkl. {p_pant:.2f} kr pant)"
                elif p_pant > 0:
                    price_label = f"{p_display:.2f} kr (+{p_pant:.2f} kr pant)"
                else:
                    price_label = f"{p_display:.2f} kr"

                st.markdown(f"""
                <div class="kpi-card {classes[idx]}">
                    <div class="kpi-title">{medals[idx]} &bull; {row['Main Category']}</div>
                    <div class="kpi-name" title="{row['Name']}">{row['Name']}</div>
                    <div class="kpi-value">{row['APK']:.4f} <span class="kpi-value-unit">ml/SEK</span></div>
                    <div class="kpi-meta">
                        <span class="kpi-badge">{row['volume']:.0f} ml</span>
                        <span class="kpi-badge">{row['alcoholPercentage']:.1f}% vol</span>
                        <span class="kpi-badge">{price_label}</span>
                    </div>
                </div>
                """, unsafe_allow_html=True)
            else:
                st.markdown(f"""
                <div class="kpi-card" style="opacity:0.3;">
                    <div class="kpi-title">{medals[idx]}</div>
                    <div class="kpi-name">-</div>
                    <div class="kpi-value">N/A</div>
                </div>
                """, unsafe_allow_html=True)
else:
    st.info("Hittade inga produkter med nuvarande filterinställningar.")

# ─── Product table ────────────────────────────────────────────────────────────
st.markdown("### Sortiment")
if not filtered_df.empty:
    display_df = pd.DataFrame({
        "Rank": filtered_df["Rank"],
        "Namn": filtered_df["Name"],
        "Kategori": filtered_df["Main Category"],
        "Underkategori": filtered_df["Subcategory"],
        "Pris (SEK)": filtered_df["Display Price"],
        "Pant (SEK)": filtered_df["Pant"],
        "Volym (ml)": filtered_df["volume"],
        "Alkohol %": filtered_df["alcoholPercentage"],
        "APK (ml/SEK)": filtered_df["APK"],
        "Beställningsvara": filtered_df["Order Assortment"],
        "Systembolaget": filtered_df["SystembolagetURL"],
    })

    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Rank": st.column_config.NumberColumn("Rank", format="#%d"),
            "Pris (SEK)": st.column_config.NumberColumn(
                "Pris inkl. pant" if include_pant else "Pris",
                format="%.2f kr"
            ),
            "Pant (SEK)": st.column_config.NumberColumn("Pant", format="%.2f kr"),
            "Volym (ml)": st.column_config.NumberColumn("Volym", format="%d ml"),
            "Alkohol %": st.column_config.NumberColumn("Alkohol", format="%.1f%%"),
            "APK (ml/SEK)": st.column_config.NumberColumn("APK", format="%.4f"),
            "Systembolaget": st.column_config.LinkColumn("Systembolaget ↗", display_text="Visa produkt ↗"),
        }
    )
else:
    st.warning("Justera dina filter för att visa resultat.")
