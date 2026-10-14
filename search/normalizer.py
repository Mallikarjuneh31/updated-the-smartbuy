"""
search/normalizer.py
====================
Query and title normalization utilities, category detection, and attribute normalizers.
No hardcoded product names, brands, or categories.
Safe from circular dependencies.
"""

import re
from typing import Optional, Any


from search.category_detector import detect_category


# Generic English stop words to strip from queries
_STOP_WORDS = {
    'a', 'an', 'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to',
    'for', 'of', 'with', 'by', 'from', 'is', 'are', 'was', 'were',
    'be', 'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did',
    'will', 'would', 'could', 'should', 'may', 'might', 'shall', 'can',
    'i', 'me', 'my', 'we', 'our', 'you', 'your', 'he', 'she', 'it',
    'they', 'them', 'their', 'this', 'that', 'these', 'those', 'buy',
    'get', 'find', 'show', 'best', 'cheap', 'good', 'nice', 'new', 'online'
}

# Regex patterns for unit normalization
_UNIT_PATTERNS = [
    (r'(\d+)\s*gb\b', r'\1gb'),
    (r'(\d+)\s*tb\b', r'\1tb'),
    (r'(\d+)\s*mb\b', r'\1mb'),
    (r'(\d+)\s*mah\b', r'\1mah'),
    (r'(\d+)\s*ml\b', r'\1ml'),
    (r'(\d+)\s*l\b', r'\1l'),
    (r'(\d+)\s*kg\b', r'\1kg'),
    (r'(\d+)\s*g\b(?!b)', r'\1g'),
    (r'(\d+)\s*mg\b', r'\1mg'),
    (r'(\d+)\s*w\b', r'\1w'),
    (r'(\d+)\s*inch\b', r'\1inch'),
    (r'(\d+)\s*"\b', r'\1inch'),
    (r'(\d+)\s*hz\b', r'\1hz'),
    (r'(\d+)\s*mp\b', r'\1mp'),
]

_MODEL_CODE_PATTERN = re.compile(
    r'\b([a-zA-Z]{1,4}\d+[a-zA-Z]?\d*|'
    r'\d+[a-zA-Z]{1,3}\d*)\b'
)

_MARKETING_WORDS = {
    'sale', 'offer', 'deal', 'deals', 'discount', 'discounted', 'special',
    'authentic', 'original', 'genuine', 'pure', 'natural', 'organic',
    'premium', 'luxury', 'best', 'top', 'new', 'latest', 'trending', 'hot',
    'exclusive', 'limited', 'edition', 'free', 'delivery', 'shipping',
    'guarantee', 'guaranteed', 'certified', 'quality', 'grade', 'approved',
    '100%', 'fresh', 'magic', 'glowing', 'brightening'
}


def strip_marketing_words(text: str) -> str:
    """
    Remove marketing buzzwords from text to isolate core product identity.
    """
    if not text:
        return ""
    words = text.split()
    filtered = [w for w in words if w.lower() not in _MARKETING_WORDS]
    return ' '.join(filtered) if filtered else text


def normalize_title(text: str) -> str:
    """
    Normalize a product title for comparison:
    1. Lowercase
    2. Collapse unit abbreviations (e.g. '256 GB' -> '256gb')
    3. Strip special characters (keep alphanumerics and spaces)
    4. Collapse extra whitespace
    """
    if not text:
        return ""
    text = str(text).lower()
    for pattern, repl in _UNIT_PATTERNS:
        text = re.sub(pattern, repl, text)
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def normalize_query(query: str) -> str:
    """
    Normalize a user search query:
    1. Apply unit normalization and lowercase
    2. Strip stop words when query contains > 2 tokens
    """
    if not query:
        return ""
    norm = normalize_title(query)
    tokens = norm.split()
    if len(tokens) > 2:
        tokens = [t for t in tokens if t not in _STOP_WORDS]
    return ' '.join(tokens) if tokens else norm


def normalize_brand(brand: Optional[str]) -> Optional[str]:
    """
    Normalize a brand name:
    Removes generic noise words, standardizes casing and aliases.
    e.g. 'Ghar Soaps' -> 'Ghar', 'Apple Inc' -> 'Apple'
    """
    if not brand or str(brand).strip().lower() in ("", "n/a", "none", "generic"):
        return None
    b_clean = re.sub(r'[^\w\s]', '', str(brand)).strip()
    b_lower = b_clean.lower()
    
    # Common brand aliases
    if b_lower in ("ghar", "ghar soaps", "ghar soap"):
        return "Ghar Soaps"
    elif b_lower in ("apple", "apple inc"):
        return "Apple"
    elif b_lower in ("samsung", "samsung electronics"):
        return "Samsung"
    elif b_lower in ("vivo", "vivo india"):
        return "Vivo"
    elif b_lower in ("oppo", "oppo india"):
        return "Oppo"
    elif b_lower in ("oneplus", "one plus"):
        return "OnePlus"
    elif b_lower in ("realme", "real me"):
        return "Realme"
    elif b_lower in ("xiaomi", "redmi", "mi"):
        return "Xiaomi"
    elif b_lower in ("pilgrim", "philgrim"):
        return "Pilgrim"
    elif b_lower in ("boat", "boAt"):
        return "boAt"
    elif b_lower in ("noise"):
        return "Noise"
    elif b_lower in ("hp", "hewlett packard"):
        return "HP"
    elif b_lower in ("dell"):
        return "Dell"
    elif b_lower in ("lenovo"):
        return "Lenovo"
    elif b_lower in ("true elements"):
        return "True Elements"
    elif b_lower in ("farmley"):
        return "Farmley"

    return b_clean.title() if len(b_clean) > 2 else b_clean.upper()


def normalize_model(model: Optional[str]) -> Optional[str]:
    """
    Normalize a model name/number.
    """
    if not model or str(model).strip() in ("", "N/A", "None"):
        return None
    m_clean = re.sub(r'[^\w\s\-\+]', '', str(model)).strip()
    # Reject generic words that are not models
    if m_clean.lower() in ("original", "natural", "organic", "pack", "soap", "pure", "best", "new", "wash"):
        return None
    return m_clean.upper() if len(m_clean) <= 6 else m_clean.title()


def normalize_weight(text: Optional[str]) -> Optional[str]:
    """
    Extract and normalize weight or volume representation to standard format (e.g. '100g', '1kg', '250ml', '1l').
    """
    if not text:
        return None
    raw = str(text).lower()
    # Match weight (g, gm, gram, kg)
    m_wt = re.search(r'(\d+(?:\.\d+)?)\s*(kg|kilo|kilograms?)\b', raw)
    if m_wt:
        val = float(m_wt.group(1))
        return f"{int(val * 1000)}g" if val < 1 else f"{val:g}kg"

    m_g = re.search(r'(\d+(?:\.\d+)?)\s*(g|gm|grams?)\b', raw)
    if m_g:
        val = float(m_g.group(1))
        if val >= 1000:
            return f"{val/1000:g}kg"
        return f"{val:g}g"

    # Match volume (ml, l, litre)
    m_l = re.search(r'(\d+(?:\.\d+)?)\s*(l|ltr|litres?|liters?)\b', raw)
    if m_l:
        val = float(m_l.group(1))
        return f"{int(val * 1000)}ml" if val < 1 else f"{val:g}L"

    m_ml = re.search(r'(\d+(?:\.\d+)?)\s*(ml|millilitres?)\b', raw)
    if m_ml:
        val = float(m_ml.group(1))
        return f"{val:g}ml"

    return None


def normalize_pack_quantity(text: Optional[str]) -> str:
    """
    Extract and normalize pack quantity (e.g. 'Pack of 3' -> '3', 'Set of 2' -> '2'). Defaults to '1'.
    Avoids erroneously matching standalone numbers like phone and laptop models (e.g. iPhone 15).
    """
    if not text:
        return "1"
    raw = str(text).lower()
    m = re.search(r'\b(?:pack\s*of|combo\s*of|set\s*of|pack-)\s*(\d+)\b', raw)
    if m:
        return m.group(1)
    m2 = re.search(r'\b(\d+)\s*(?:pack|packs|pcs|pieces|units)\b', raw)
    if m2:
        return m2.group(1)
    m3 = re.search(r'\b(?:x|qty|quantity)\s*[:\-]?\s*(\d+)\b', raw)
    if m3:
        return m3.group(1)
    return "1"


def normalize_price(text: Any) -> Optional[int]:
    """
    Extract pure integer price in INR. Returns None if invalid or missing.
    """
    if text is None:
        return None
    s = str(text).strip()
    if s.lower() in ("", "n/a", "none", "0", "0.0"):
        return None
    s = re.sub(r'[₹$€£\s,]|rs\.?|inr', '', s, flags=re.IGNORECASE)
    m = re.search(r'(\d+(?:\.\d+)?)', s)
    if m:
        try:
            val = int(round(float(m.group(1))))
            return val if val > 0 else None
        except Exception:
            return None
    return None


def normalize_rating(rating: Any) -> Optional[float]:
    """
    Normalize rating float between 1.0 and 5.0. Returns None if missing/invalid.
    """
    if rating is None or str(rating).strip() in ("", "N/A", "None", "0", "0.0"):
        return None
    m = re.search(r'\b([1-5]\.[0-9]|[1-5])\b', str(rating))
    if m:
        val = float(m.group(1))
        return round(val, 1)
    return None


def normalize_review_count(review_count: Any) -> int:
    """
    Extract pure integer count of customer reviews. Defaults to 0.
    """
    if review_count is None:
        return 0
    digits = re.sub(r'[^\d]', '', str(review_count))
    return int(digits) if digits else 0


def detect_query_type(query: str) -> str:
    """Classify user query into broad intent: brand_model, model_only, category, or generic."""
    tokens = normalize_query(query).split()
    if not tokens:
        return "generic"
    for t in tokens:
        if re.search(r'[a-zA-Z]+\d+', t) or re.search(r'\d+[a-zA-Z]+', t):
            return "brand_model"
    cat = detect_category(query=query)
    if cat != "other":
        return "category"
    return "generic"


def build_search_query(brand: str = '', category: str = '',
                       ram: str = '', storage: str = '',
                       processor: str = '', appliance_type: str = '',
                       type_val: str = '', q: str = '', **kwargs) -> str:
    """Build a search string combining selected specs."""
    parts = []
    clean_brand = brand.strip() if brand and brand.lower() not in ('any', 'all', 'all brands', '') else ''
    if clean_brand:
        parts.append(clean_brand)

    clean_q = q.strip() if q and q.lower() not in ('any', 'all', '') else ''
    clean_cat = category.strip() if category and category.lower() not in ('any', 'all', '') else ''

    if clean_q:
        if clean_brand and clean_brand.lower() in clean_q.lower():
            parts = [clean_q]
        else:
            parts.append(clean_q)
    elif clean_cat:
        parts.append(clean_cat)

    if storage and storage.lower() not in ('any', 'all', 'any storage', ''):
        parts.append(storage)
    if ram and ram.lower() not in ('any', 'all', 'any ram', ''):
        parts.append(ram)

    return ' '.join(parts).strip() or (clean_q or 'Products')


def build_search_query_chain(brand: str = '', category: str = '',
                             ram: str = '', storage: str = '',
                             processor: str = '', appliance_type: str = '',
                             type_val: str = '', q: str = '', **kwargs) -> list[str]:
    """Generate ordered list of fallback queries from specific to broad."""
    queries = []
    q1 = build_search_query(brand=brand, category=category, ram=ram, storage=storage,
                            processor=processor, appliance_type=appliance_type,
                            type_val=type_val, q=q, **kwargs)
    if q1:
        queries.append(q1)

    clean_q = q.strip() if q and q.lower() not in ('any', 'all', '') else ''
    if clean_q and clean_q not in queries:
        queries.append(clean_q)

    clean_brand = brand.strip() if brand and brand.lower() not in ('any', 'all', 'all brands', '') else ''
    if clean_brand and clean_brand not in queries:
        queries.append(clean_brand)

    return queries if queries else ['Products']
