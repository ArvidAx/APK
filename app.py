import streamlit as st
import pandas as pd
import httpx
import json
import os
import logging
import datetime
import altair as alt
from typing import Any, Dict, List, Union

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

# Premium clean SaaS CSS layout (non-slop, clean minimal style)
st.markdown("""
<style>
    /* Clean minimal modifications */
    .stApp {
        background-color: #0b0f19;
        color: #f1f5f9;
    }
    h1, h2, h3 {
        font-family: 'Inter', sans-serif !important;
        font-weight: 600 !important;
    }
    
    /* Modern flat KPI Cards container */
    .kpi-container {
        display: flex;
        gap: 1rem;
        margin-bottom: 1.5rem;
        margin-top: 0.75rem;
    }
    
    .kpi-card {
        flex: 1;
        background: #111827;
        border-radius: 8px;
        padding: 1.25rem;
        border: 1px solid #1f2937;
        border-left: 4px solid #3b82f6;
        transition: border-color 0.2s ease;
    }
    
    .kpi-card:hover {
        border-color: #4b5563;
    }
    
    /* Subtle borders for ranks */
    .kpi-card.gold {
        border-left-color: #d97706;
    }
    .kpi-card.silver {
        border-left-color: #6b7280;
    }
    .kpi-card.bronze {
        border-left-color: #b45309;
    }
    
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
    """
    Checks if a product is a order assortment item ('Beställningsvara') based on assortment properties.
    """
    assort = str(row.get("assortment") or "").strip().upper()
    assort_text = str(row.get("assortmentText") or "").strip().lower()
    return (assort == "BS") or ("beställningssortiment" in assort_text)


def construct_name(row: pd.Series, name_col: str, add_name_col: str) -> str:
    """
    Constructs the clean display name by concatenating Name and AdditionalName if available.
    """
    n = row.get(name_col)
    an = row.get(add_name_col)
    
    n_str = "" if pd.isna(n) or n is None else str(n).strip()
    an_str = "" if pd.isna(an) or an is None else str(an).strip()
    
    if an_str and an_str.lower() != "nan" and an_str != "":
        return f"{n_str} ({an_str})"
    return n_str


def get_pant_sek(row: pd.Series) -> float:
    """
    Calculates the Swedish deposit (pant) fee in SEK based on packaging type and recycleFee.
    - Cans (Burk): 2.00 SEK (All metal cans have 2 kr pant)
    - PET bottles (PET-flaska): 2.00 SEK (All PET bottles have 2 kr pant as requested)
    - Standard returnable glass (Returglas): 0.60 SEK (<=33cl) or 0.90 SEK (>33cl)
    """
    packaging = str(row.get("bottleText") or "").strip().lower()
    
    # Prioritize metal can (burk) and PET bottle checks so they have exactly 2.00 SEK pant
    if "burk" in packaging or "pet" in packaging:
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
                if fee_val > 10.0:
                    return fee_val / 100.0
                return fee_val
        except ValueError:
            pass
            
    # Fallback based on packaging text
    if "returglas" in packaging:
        vol = float(row.get("volume") or 0)
        return 0.60 if vol <= 330 else 0.90
        
    return 0.00


def get_systembolaget_url(row: pd.Series) -> str:
    """
    Constructs the official Systembolaget product page URL dynamically.
    Fails safe to Systembolaget search page if required fields are missing.
    """
    prod_num = str(row.get("productNumber") or "").strip()
    if not prod_num:
        return "https://www.systembolaget.se/"
        
    cat = str(row.get("categoryLevel1") or "").strip().lower()
    cat_map = {
        "öl": "ol",
        "sprit": "sprit",
        "vin": "vin",
        "cider & blanddrycker": "cider-och-blanddrycker",
        "alkoholfritt": "alkoholfritt",
        "presenter": "presenter"
    }
    cat_slug = cat_map.get(cat, cat)
    
    # Replace Swedish characters in category
    cat_slug = cat_slug.replace("ö", "o").replace("ä", "a").replace("å", "a").replace("&", "och").replace(" ", "-")
    
    # Clean product name for slug
    name = str(row.get("Name") or "").strip().lower()
    name_slug = name.replace("ö", "o").replace("ä", "a").replace("å", "a")
    
    # Keep only alphanumeric characters and replace spaces/punctuation with hyphens
    import re
    name_slug = re.sub(r'[^a-z0-9\-]', '-', name_slug)
    name_slug = re.sub(r'-+', '-', name_slug).strip('-')
    
    return f"https://www.systembolaget.se/produkt/{cat_slug}/{name_slug}-{prod_num}/"


# Caching utility helpers completed successfully.


@st.cache_data(ttl=86400)
def load_data() -> pd.DataFrame:
    """
    Loads Systembolaget assortment data. 
    First checks if 'local_products_fallback.json' exists and is fresher than 24 hours (86400s).
    If so, loads instantly from local disk. Otherwise, fetches fresh data from API mirror
    and updates the local file on disk.
    """
    data = None
    loaded_from_cache = False
    fetched_from_api = False

    # Check if local cache file is fresh (modified within the last 24 hours)
    if os.path.exists(FALLBACK_FILE):
        try:
            mtime = os.path.getmtime(FALLBACK_FILE)
            now = datetime.datetime.now().timestamp()
            if now - mtime < 86400:
                logger.info("Local fallback cache is fresh (<24h old). Loading directly from disk...")
                with open(FALLBACK_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                loaded_from_cache = True
                logger.info("Successfully loaded assortment from fresh local disk cache.")
        except Exception as fe:
            logger.error(f"Failed to read local cache file: {fe}")

    # If no fresh local cache, fetch from remote API mirror
    if not loaded_from_cache:
        try:
            logger.info(f"Attempting to fetch fresh assortment from API mirror: {API_URL}")
            with httpx.Client(timeout=30.0) as client:
                response = client.get(API_URL)
                response.raise_for_status()
                data = response.json()
                fetched_from_api = True
                logger.info("Assortment successfully fetched from API mirror.")
        except Exception as e:
            logger.error(f"Failed to fetch data from API mirror: {e}")
            # If API fails, try to load any available local fallback even if older than 24 hours!
            if os.path.exists(FALLBACK_FILE):
                logger.info("API unreachable. Loading available local cache file (even if older than 24 hours)...")
                try:
                    with open(FALLBACK_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    logger.info("Successfully loaded assortment from local fallback.")
                except Exception as fe:
                    logger.error(f"Failed to read local fallback: {fe}")
                    raise RuntimeError(f"API fetch failed, and local backup file is corrupted: {fe}")
            else:
                raise RuntimeError(
                    f"API fetch failed and no local fallback file '{FALLBACK_FILE}' exists to recover."
                )

    if not data:
        raise ValueError("Decoded assortment payload is empty.")

    # 2. Data Processing & Cleaning
    df = pd.DataFrame(data)
    if df.empty:
        raise ValueError("Consolidated assortment contains no active entries.")

    # Ensure vital assortment fields are present
    vital_cols = [
        "price", "volume", "alcoholPercentage", "categoryLevel1", 
        "categoryLevel2", "assortment", "assortmentText", "producerName"
    ]
    for col in vital_cols:
        if col not in df.columns:
            df[col] = None

    # Strict numeric conversions
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["alcoholPercentage"] = pd.to_numeric(df["alcoholPercentage"], errors="coerce")

    # Drop rows based on strict validation pipeline:
    # Price, volume, and alcohol content must be valid (> 0)
    initial_rows = len(df)
    df = df.dropna(subset=["price", "volume", "alcoholPercentage"])
    df = df[(df["price"] > 0) & (df["volume"] > 0) & (df["alcoholPercentage"] > 0)]
    cleaned_rows = len(df)
    logger.info(f"Ingestion Pipeline: Cleaned dataset rows from {initial_rows} to {cleaned_rows}.")

    # Robust Name parsing supporting productNameBold & productNameThin
    name_col = "productNameBold" if "productNameBold" in df.columns else "name"
    add_name_col = "productNameThin" if "productNameThin" in df.columns else "additionalName"
    
    df["Name"] = df.apply(lambda r: construct_name(r, name_col, add_name_col), axis=1)

    # Categories assignment
    df["Main Category"] = df["categoryLevel1"].fillna("Övrigt").astype(str).str.strip()
    df["Subcategory"] = df["categoryLevel2"].fillna("Övrigt").astype(str).str.strip()

    # Order Assortment flags
    df["IsOrderAssortment"] = df.apply(is_order_item, axis=1)
    df["Order Assortment"] = df["IsOrderAssortment"].map({True: "Ja", False: "Nej"})
    df["Producer"] = df["producerName"].fillna("Okänd").astype(str).str.strip()

    # Calculate Pant & URL
    df["Pant"] = df.apply(get_pant_sek, axis=1)
    df["SystembolagetURL"] = df.apply(get_systembolaget_url, axis=1)

    # 3. APK Algorithmic Calculation
    # Formula: APK = (Volume * (AlcoholPercentage / 100)) / Price
    df["APK"] = (df["volume"] * (df["alcoholPercentage"] / 100.0)) / df["price"]
    df["APK"] = df["APK"].round(4)

    # Base sort descending by APK
    df = df.sort_values(by="APK", ascending=False).reset_index(drop=True)
    
    # Deduplicate: Keep only the first entry (highest APK) for any given Name, Volume, and Alcohol Percentage
    initial_dedup = len(df)
    df = df.drop_duplicates(subset=["Name", "volume", "alcoholPercentage"], keep="first").reset_index(drop=True)
    deduped_rows = len(df)
    logger.info(f"Deduplication Pipeline: Reduced rows from {initial_dedup} to {deduped_rows} by grouping Name, volume, and alcoholPercentage.")
    
    # Establish overall absolute rank
    df["Rank"] = df.index + 1

    # Save to local fallback cache dynamically if fetched successfully from API
    if fetched_from_api:
        try:
            with open(FALLBACK_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info("Successfully updated local fallback cache file on disk for offline resilience.")
        except Exception as se:
            logger.warning(f"Failed to update local cache file on disk: {se}")

    return df


# Main execution flow
try:
    df = load_data()
except Exception as exc:
    st.error("### Systemfel: Assortment kunde inte hämtas ⚠️")
    st.markdown(f"""
    Applikationen misslyckades med att hämta Systembolagets produktdata från API:t och kunde inte ladda en lokal backup.
    
    **Felmeddelande:**
    `{str(exc)}`
    
    *Vänligen kontrollera din internetanslutning eller kontakta administratören.*
    """)
    st.stop()

# Header Component
st.title("Systembolaget APK-Analysator")
today_str = datetime.date.today().strftime("%Y-%m-%d")
st.markdown(
    f"Sök, filtrera och analysera prisvärdheten på Systembolagets sortiment baserat på **APK (Alkohol Per Krona)**. "
    f"Data uppdateras dagligen. Senaste körning: {today_str}"
)

# Status info bar
st.sidebar.markdown("### Systemstatus")
st.sidebar.info(
    f"Totalt sortiment: {len(df):,} produkter laddade och validerade."
)

# Sidebar Control Panel
st.sidebar.markdown("### Filter")

# Pant toggle switch
include_pant = st.sidebar.toggle(
    "Inkludera pant i priset",
    value=False,
    help="I Sverige betalas pant utöver hyllpriset på burkar, PET-flaskor och returglas. Aktivera för att beräkna APK baserat på det fulla priset (bruttopris) du betalar vid kassan."
)

# 1. Category Filter (default pre-select Öl and Sprit)
categories = sorted(list(df["Main Category"].unique()))
default_cats = [c for c in ["Öl", "Sprit"] if c in categories]
selected_categories = st.sidebar.multiselect(
    "Välj Huvudkategorier",
    options=categories,
    default=default_cats,
    help="Välj en eller flera kategorier att analysera."
)

# 2. Order Assortment Filter (Visa beställningsvaror)
show_order_items = st.sidebar.checkbox(
    "Visa beställningsvaror (BS)",
    value=False,
    help="Markera för att inkludera beställningssortimentet. Avmarkerad visar endast direkt tillgängliga hyllvaror."
)

# 3. Dynamic Range Sliders setup
min_alc_val = float(df["alcoholPercentage"].min())
max_alc_val = float(df["alcoholPercentage"].max())
selected_alc = st.sidebar.slider(
    "Alkoholhalt (%)",
    min_value=min_alc_val,
    max_value=max_alc_val,
    value=(min_alc_val, max_alc_val),
    step=0.5,
    format="%.1f%%"
)

# Generate options for the select_slider
min_price_val = float(df["price"].min())
max_price_val = float(df["price"].max())

# Linear increments of 5 kr under 1000 SEK
options_under_1000 = list(range(int(min_price_val), min(1000, int(max_price_val)) + 1, 5))
if int(min_price_val) not in options_under_1000:
    options_under_1000.insert(0, int(min_price_val))

if max_price_val > 1000.0:
    # Increments of 50 kr from 1000 to 5000 SEK, and 250 kr above 5000 SEK
    options_above_1000 = list(range(1000, min(5000, int(max_price_val)) + 1, 50))
    if max_price_val > 5000.0:
        options_above_1000 += list(range(5000, int(max_price_val) + 1, 250))
    if int(max_price_val) not in options_above_1000:
        options_above_1000.append(int(max_price_val))
    options = sorted(list(set(options_under_1000 + options_above_1000)))
else:
    options = sorted(list(set(options_under_1000)))

# Set up the non-linear select_slider with exact price handles
default_high_val = min(500, options[-1]) if options[-1] > 500 else options[-1]
if default_high_val not in options:
    default_high_val = min(options, key=lambda x: abs(x - default_high_val))

selected_price_low, selected_price_high = st.sidebar.select_slider(
    "Pris (SEK)",
    options=options,
    value=(options[0], default_high_val),
    format_func=lambda val: f"{val} kr"
)

# 4. Text Search input
search_query = st.sidebar.text_input(
    "Sök produkt eller producent",
    placeholder="T.ex. Falcon, Absolut, Bordeaux...",
    help="Sökningen matchar produktnamn, ytterligare beskrivning samt producentnamn skiftlägesoberoende."
)

# Apply filters
filtered_df = df.copy()

# Dynamic Price & APK adjustment based on pant toggle
if include_pant:
    filtered_df["Display Price"] = filtered_df["price"] + filtered_df["Pant"]
else:
    filtered_df["Display Price"] = filtered_df["price"]

# Recalculate APK dynamically on display price
filtered_df["APK"] = (filtered_df["volume"] * (filtered_df["alcoholPercentage"] / 100.0)) / filtered_df["Display Price"]
filtered_df["APK"] = filtered_df["APK"].round(4)

# Filter 1: Main Category
if selected_categories:
    filtered_df = filtered_df[filtered_df["Main Category"].isin(selected_categories)]

# Filter 2: Order Assortment
if not show_order_items:
    filtered_df = filtered_df[filtered_df["IsOrderAssortment"] == False]

# Filter 3: Alcohol Range
filtered_df = filtered_df[
    (filtered_df["alcoholPercentage"] >= selected_alc[0]) & 
    (filtered_df["alcoholPercentage"] <= selected_alc[1])
]

# Filter 4: Price Range (using mapped exponential values)
filtered_df = filtered_df[
    (filtered_df["price"] >= selected_price_low) & 
    (filtered_df["price"] <= selected_price_high)
]

# Filter 5: Search Query
if search_query:
    q = search_query.strip().lower()
    filtered_df = filtered_df[
        filtered_df["Name"].str.lower().str.contains(q, na=False) |
        filtered_df["Producer"].str.lower().str.contains(q, na=False)
    ]

# Sort explicitly by the newly calculated APK
filtered_df = filtered_df.sort_values(by="APK", ascending=False).reset_index(drop=True)
filtered_df["Rank"] = filtered_df.index + 1

# Render KPI Top 3 cards based on active filtered dataset (Clean minimal layout)
st.markdown("### Mest prisvärda produkter")
if not filtered_df.empty:
    top_3 = filtered_df.head(3)
    
    # HTML KPI Columns container
    kpi_cols = st.columns(3)
    medals = ["Rank 1", "Rank 2", "Rank 3"]
    classes = ["gold", "silver", "bronze"]
    
    for idx in range(3):
        with kpi_cols[idx]:
            if idx < len(top_3):
                row = top_3.iloc[idx]
                
                # Format price label showing pant context
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
                    <div class="kpi-title">{medals[idx]} • {row['Main Category']}</div>
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
                <div class="kpi-card" style="border-left-color: rgba(255, 255, 255, 0.05); opacity: 0.4;">
                    <div class="kpi-title">{medals[idx]}</div>
                    <div class="kpi-name">-</div>
                    <div class="kpi-value">N/A</div>
                    <div class="kpi-meta">-</div>
                </div>
                """, unsafe_allow_html=True)
else:
    st.info("Hittade inga produkter med nuvarande filterinställningar.")

# Main Interactive Dataframe View
st.markdown("### Sortiment")
if not filtered_df.empty:
    # Prepare clean presentation schema
    display_df = pd.DataFrame({
        "Rank": filtered_df["Rank"],
        "Name": filtered_df["Name"],
        "Main Category": filtered_df["Main Category"],
        "Subcategory": filtered_df["Subcategory"],
        "Price (SEK)": filtered_df["Display Price"],
        "Pant (SEK)": filtered_df["Pant"],
        "Volume (ml)": filtered_df["volume"],
        "Alcohol %": filtered_df["alcoholPercentage"],
        "APK (ml/SEK)": filtered_df["APK"],
        "Order Assortment (Yes/No)": filtered_df["Order Assortment"],
        "Systembolaget": filtered_df["SystembolagetURL"]
    })

    # Render optimized st.dataframe
    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Rank": st.column_config.NumberColumn("Rank", format="#%d"),
            "Price (SEK)": st.column_config.NumberColumn(
                "Pris inkl. pant (SEK)" if include_pant else "Pris exkl. pant (SEK)", 
                format="%.2f kr"
            ),
            "Pant (SEK)": st.column_config.NumberColumn("Pant", format="%.2f kr"),
            "Volume (ml)": st.column_config.NumberColumn("Volym (ml)", format="%d ml"),
            "Alcohol %": st.column_config.NumberColumn("Alkoholhalt (%)", format="%.1f%%"),
            "APK (ml/SEK)": st.column_config.NumberColumn("APK (ml/SEK)", format="%.4f"),
            "Systembolaget": st.column_config.LinkColumn("Systembolaget ↗", display_text="Visa produkt ↗")
        }
    )

else:
    st.warning("Justera dina filter i kontrollpanelen för att visa resultat.")
