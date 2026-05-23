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
    - PET-flaska: 2.00 SEK
    - Returglas: 0.60 SEK (<=33cl) or 0.90 SEK (>33cl)
    """
    packaging = str(row.get("bottleText") or "").strip().lower()
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

categories = sorted(df["Main Category"].unique().tolist())
default_cats = [c for c in ["Öl", "Sprit"] if c in categories]
selected_categories = st.sidebar.multiselect(
    "Välj kategorier",
    options=categories,
    default=default_cats,
)

show_order_items = st.sidebar.checkbox(
    "Visa beställningsvaror (BS)",
    value=False,
)

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

# Exponential price slider
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

selected_price_low, selected_price_high = st.sidebar.select_slider(
    "Pris (SEK)",
    options=price_options,
    value=(price_options[0], default_high),
    format_func=lambda val: f"{val} kr"
)

search_query = st.sidebar.text_input(
    "Sök produkt eller producent",
    placeholder="T.ex. Falcon, Absolut, Bordeaux...",
)

# ─── Apply filters ────────────────────────────────────────────────────────────
filtered_df = df.copy()

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

if search_query:
    q = search_query.strip().lower()
    filtered_df = filtered_df[
        filtered_df["Name"].str.lower().str.contains(q, na=False) |
        filtered_df["Producer"].str.lower().str.contains(q, na=False)
    ]

filtered_df = filtered_df.sort_values(by="APK", ascending=False).reset_index(drop=True)
filtered_df["Rank"] = filtered_df.index + 1

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
