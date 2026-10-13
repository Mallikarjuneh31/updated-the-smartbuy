import re
from typing import Any, Optional, Dict, List, Union
from search.category_detector import detect_category


def safe_number(val, default: float = 0.0) -> float:
    """Safely convert any numeric value, string, or None to float."""
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    try:
        val_str = str(val).strip()
        if not val_str or val_str.upper() in ('N/A', 'NONE', 'NOT AVAILABLE', 'NULL', ''):
            return default
        clean_str = val_str.replace(',', '')
        m = re.search(r'(\d+(?:\.\d+)?)', clean_str)
        return float(m.group(1)) if m else default
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Regex patterns for universal spec extraction
# ---------------------------------------------------------------------------

_SPEC_PATTERNS = {
    # Electronics
    'ram':       [r'\b(\d+)\s*gb\s*(?:ram|lpddr\d?)',
                  r'\b(4|6|8|12|16|32|64)\s*gb\b'],
    'storage':   [r'\b(\d+)\s*(?:gb|tb)\s*(?:rom|ssd|hdd|emmc|storage|internal)',
                  r'\b(64|128|256|512|1024)\s*gb\b',
                  r'\b(1|2|4)\s*tb\b'],
    'battery':   [r'\b(\d{4,5})\s*mah\b'],
    'display':   [r'\b(\d{1,2}(?:\.\d{1,2})?)\s*(?:inch|"|-inch)\b'],
    'processor': [r'\b(snapdragon[\s\w]*?(?:gen\s*\d)?|'
                  r'mediatek[\s\w]*?|dimensity[\s\w]*?|'
                  r'helio[\s\w]*?|bionic[\s\w]*?|'
                  r'core\s*i[3579]|ryzen\s*\d|'
                  r'a1[4-9]|m[1-4])\b'],
    'network':   [r'\b(5g|4g|3g|lte|wifi\s*6e?)\b'],
    'os':        [r'\b(android\s*\d+|ios\s*\d+|windows\s*\d+|'
                  r'miui\s*\d+|one\s*ui\s*\d+)\b'],

    # Weights & volumes (Grocery, Fitness, Beauty)
    'weight':    [r'\b(\d+(?:\.\d+)?)\s*(kg|g|gm|grams?|kilograms?)\b'],
    'volume':    [r'\b(\d+(?:\.\d+)?)\s*(ml|l|litre?s?|liter?s?)\b'],
    'quantity':  [r'\b(?:pack\s*of\s*|qty\s*:?\s*|x\s*)(\d+)\b',
                  r'\b(\d+)\s*(?:pieces?|pcs?|tablets?|capsules?|sachets?|units?)\b'],
    'capacity':  [r'\b(\d+(?:\.\d+)?)\s*(litres?|liters?|l)\b'],

    # Clothing & Footwear
    'size':      [r'\bsize[:\s]+([xsl]{1,3}|\d{1,3}(?:\.\d)?)\b',
                  r'\b(xs|s\b|m\b|l\b|xl|xxl|xxxl)\b'],
    'color':     [r'\b(black|white|red|blue|green|yellow|pink|purple|'
                  r'grey|gray|silver|gold|brown|orange|cyan|magenta|'
                  r'navy|maroon|beige|cream|coral|rose|turquoise)\b'],

    # Furniture / Appliances
    'material':  [r'\b(wood|wooden|metal|steel|aluminum|aluminium|'
                  r'plastic|fabric|leather|glass|rubber|ceramic|'
                  r'cotton|polyester|nylon|bamboo|silicone)\b'],
    'wattage':   [r'\b(\d+)\s*(?:w|watts?)\b'],
    'voltage':   [r'\b(\d+)\s*(?:v|volts?)\b'],

    # Books
    'author':    [r'\bby\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b'],
    'pages':     [r'\b(\d+)\s*pages?\b'],
}

# Colors list for dedicated color detection
_COLORS = [
    'Black', 'White', 'Red', 'Blue', 'Green', 'Yellow', 'Pink',
    'Purple', 'Grey', 'Gray', 'Silver', 'Gold', 'Brown', 'Orange',
    'Cyan', 'Magenta', 'Navy', 'Maroon', 'Beige', 'Cream', 'Coral',
    'Rose', 'Turquoise', 'Midnight', 'Starlight', 'Graphite',
    'Titanium', 'Sage', 'Lavender', 'Olive', 'Khaki',
]

_COLOR_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(c) for c in _COLORS) + r')\b', re.I
)

_RAM_ONLY_PATTERN = re.compile(
    r'(\d+)\s*gb\s*\+\s*(\d+)\s*gb',
    re.I
)

_MODEL_CODE_PATTERN = re.compile(
    r'\b([a-zA-Z]{1,4}\d+[a-zA-Z]?\d*|'   # e.g. S24, A55, M34, i7, RX7
    r'\d+[a-zA-Z]{1,3}\d*)\b'              # e.g. 12Pro, 15Plus
)


def _extract_brand_from_title(title: str) -> str:
    """Heuristic brand extraction: the first capitalized proper noun."""
    non_brands = {
        'the', 'a', 'an', 'new', 'best', 'top', 'premium', 'original',
        'genuine', 'pack', 'set', 'combo', 'for', 'with', 'and', 'or',
        'in', 'on', 'by', 'of', 'size', 'color', 'model',
        'certified', 'refurbished', 'used', 'open', 'box',
    }
    tokens = title.split()
    for token in tokens:
        clean = re.sub(r'[^a-zA-Z0-9]', '', token)
        if (clean and len(clean) >= 2
                and clean[0].isupper()
                and clean.lower() not in non_brands
                and not clean.isdigit()):
            return clean
    return 'Not Available'




def extract_specs(item: dict, query: str = "") -> dict:
    """
    Dynamically extract specifications and category details from a product item.
    Returns structured specs dictionary including category, model, seller, warranty, and category specs.
    """
    title = item.get('title', '')
    title_lower = title.lower()
    specs: dict[str, Any] = {k: 'N/A' for k in _SPEC_PATTERNS}
    specs['brand']      = 'N/A'
    specs['color']      = 'N/A'
    specs['rating_num'] = 0.0
    specs['price_num']  = item.get('price_num')

    # Brand
    specs['brand'] = _extract_brand_from_title(title)

    # Category
    category = detect_category(query=query, title=title)
    specs['category'] = category

    # Model Number / Code
    m_code = _MODEL_CODE_PATTERN.search(title)
    specs['model_number'] = m_code.group(1).upper() if m_code else 'Not Available'

    # Color
    color_match = _COLOR_PATTERN.search(title)
    if color_match:
        specs['color'] = color_match.group(1).capitalize()

    # RAM + Storage
    combined_match = _RAM_ONLY_PATTERN.search(title)
    if combined_match:
        specs['ram']     = f"{combined_match.group(1)} GB"
        specs['storage'] = f"{combined_match.group(2)} GB"

    # All other patterns
    for spec_key, patterns in _SPEC_PATTERNS.items():
        if specs.get(spec_key) not in (None, 'N/A'):
            continue

        for pattern in patterns:
            m = re.search(pattern, title_lower, re.I)
            if m:
                val = m.group(0).strip()
                if spec_key in ('ram', 'storage', 'battery', 'weight',
                                'volume', 'wattage', 'voltage', 'pages'):
                    val = m.group(1).strip()
                    if spec_key == 'ram':
                        val = f"{val} GB"
                    elif spec_key == 'storage':
                        unit = 'TB' if 'tb' in m.group(0).lower() else 'GB'
                        val = f"{val} {unit}"
                    elif spec_key == 'battery':
                        val = f"{val} mAh"
                    elif spec_key == 'display':
                        val = f'{val}"'
                    elif spec_key in ('weight', 'volume', 'capacity'):
                        unit = m.group(2).strip() if len(m.groups()) >= 2 else ''
                        val = f"{val} {unit}".strip()
                    elif spec_key == 'wattage':
                        val = f"{val} W"
                    elif spec_key == 'voltage':
                        val = f"{val} V"

                specs[spec_key] = val.strip()
                break

    # Rating
    specs['rating_num'] = safe_number(item.get('rating'))

    # Camera heuristic
    cam_match = re.search(r'\b(\d{2,3}\s*mp|\d+\s*\+\s*\d+\s*mp)\b', title_lower)
    specs['camera'] = cam_match.group(1).upper() if cam_match else ('Not Available' if category == 'Smartphones' else 'N/A')

    # 5G Support
    specs['is_5g'] = 'Yes (5G Ready)' if '5g' in title_lower else ('No' if category == 'Smartphones' else 'N/A')

    # Seller & Delivery & Warranty
    specs['seller']   = item.get('seller') if item.get('seller') and item.get('seller') != 'N/A' else 'Not Available'
    price_val         = safe_number(item.get('price_num'))
    specs['delivery'] = 'Free Delivery (2-3 Days)' if price_val > 499 else 'Standard Delivery'
    warr_raw = item.get('warranty')
    if warr_raw and str(warr_raw).lower() not in ('n/a', 'none', 'not available'):
        specs['warranty'] = str(warr_raw)
    elif category in ('phone', 'Smartphones', 'laptop', 'Laptops', 'Televisions', 'Refrigerators', 'Washing Machines', 'kitchen', 'home_appliance'):
        specs['warranty'] = '1 Year Brand Warranty'
    else:
        specs['warranty'] = 'Standard Return Policy'

    # Build category-tailored specifications list
    cat_specs = {}
    c_norm = str(category or "").lower()

    if c_norm in ("phone", "smartphones", "mobile"):
        cat_specs["Display"] = specs.get('display', 'Not Available')
        cat_specs["Processor"] = specs.get('processor', 'Not Available')
        cat_specs["RAM"] = specs.get('ram', 'Not Available')
        cat_specs["Storage"] = specs.get('storage', 'Not Available')
        cat_specs["Camera"] = specs.get('camera', 'Not Available')
        cat_specs["Battery"] = specs.get('battery', 'Not Available')
        cat_specs["OS"] = specs.get('os', 'Android / iOS')
        cat_specs["5G Support"] = specs.get('is_5g', 'Not Available')
        cat_specs["Color"] = specs.get('color', 'Not Available')
    elif c_norm in ("laptop", "laptops"):
        cat_specs["Processor"] = specs.get('processor', 'Not Available')
        cat_specs["RAM"] = specs.get('ram', 'Not Available')
        cat_specs["SSD/Storage"] = specs.get('storage', 'Not Available')
        cat_specs["Display Size"] = specs.get('display', 'Not Available')
        cat_specs["Graphics"] = "Integrated / Dedicated" if ('nvidia' in title_lower or 'rtx' in title_lower) else "Integrated Graphics"
        cat_specs["OS"] = specs.get('os', 'Windows 11 Home')
        cat_specs["Weight"] = specs.get('weight', 'Not Available')
    elif c_norm in ("face_wash", "face wash"):
        cat_specs["Product Type"] = "Face Wash"
        v_m = re.search(r'\b(vitamin c|salicylic acid|tea tree|neem|aloe vera|charcoal|hyaluronic|papaya|coffee|ubtan|glycolic)\b', title_lower)
        cat_specs["Active Ingredient"] = v_m.group(1).title() if v_m else "Natural Cleansing Extracts"
        cat_specs["Skin Type"] = "Oily / Acne Prone" if any(w in title_lower for w in ['acne', 'pimple', 'oil']) else ("Dry Skin" if 'dry' in title_lower else "All Skin Types")
        vol_m = re.search(r'(\d+\s*(?:ml|g))\b', title_lower)
        cat_specs["Volume / Weight"] = vol_m.group(1) if vol_m else specs.get('volume', '100 ml')
        cat_specs["Benefits"] = "Deep Cleansing & Oil Control" if ('oil' in title_lower or 'acne' in title_lower) else "Glowing & Hydrated Skin"
    elif c_norm in ("soap",):
        cat_specs["Product Type"] = "Bathing Soap"
        w_m = re.search(r'(\d+\s*g)\b', title_lower)
        cat_specs["Weight"] = w_m.group(1) if w_m else specs.get('weight', '100g')
        pq_m = re.search(r'(?:pack of|pack|pack-)(\d+)', title_lower)
        cat_specs["Pack Quantity"] = f"Pack of {pq_m.group(1)}" if pq_m else "Pack of 1"
        cat_specs["Fragrance"] = "Sandalwood / Herbal" if any(w in title_lower for w in ['sandal', 'herbal', 'ayurvedic', 'magic']) else "Refreshing Fragrance"
        cat_specs["Skin Type"] = "All Skin Types"
    elif c_norm in ("grocery", "food", "chia seeds", "groceries"):
        cat_specs["Product Type"] = "Chia Seeds / Superfood" if 'chia' in title_lower else "Grocery Food Item"
        w_m = re.search(r'(\d+\s*(?:g|kg))\b', title_lower)
        cat_specs["Weight"] = w_m.group(1) if w_m else specs.get('weight', '500g')
        pq_m = re.search(r'(?:pack of|pack|pack-)(\d+)', title_lower)
        cat_specs["Pack Quantity"] = f"Pack of {pq_m.group(1)}" if pq_m else "Pack of 1"
        cat_specs["Ingredients"] = "100% Whole Raw Chia Seeds" if 'chia' in title_lower else "Natural Ingredients"
        cat_specs["Organic"] = "Certified Organic" if 'organic' in title_lower else "100% Natural"
        cat_specs["Diet Type"] = "Gluten Free, Vegan, High Fiber, Omega-3"
    elif c_norm in ("clothing", "shoes", "fashion"):
        sz_m = re.search(r'\b(s|m|l|xl|xxl|2xl|3xl|32|34|36|38|40|42)\b', title_lower)
        cat_specs["Size"] = sz_m.group(1).upper() if sz_m else specs.get('size', 'Standard Size')
        cat_specs["Color"] = specs.get('color', 'Standard Variant')
        cat_specs["Material"] = "100% Cotton / Denim" if any(w in title_lower for w in ['cotton', 'denim']) else specs.get('material', 'Premium Fabric')
        cat_specs["Pattern"] = "Solid" if 'solid' in title_lower else ("Printed" if 'printed' in title_lower else "Casual Regular")
        cat_specs["Fit Type"] = "Slim Fit" if 'slim' in title_lower else ("Regular Fit" if 'regular' in title_lower else "Standard Fit")
    elif c_norm in ("bags", "luggage", "backpack"):
        cat_specs["Product Type"] = "Trolley Bag / Suitcase" if any(w in title_lower for w in ['trolley', 'luggage', 'suitcase']) else ("Backpack" if 'backpack' in title_lower else "Bag")
        cap_m = re.search(r'(\d{2,3})\s*(?:l|ltr|litres?|liters?)\b', title_lower)
        cat_specs["Capacity"] = f"{cap_m.group(1)} Litres" if cap_m else specs.get('capacity', 'Not Available')
        sz_m = re.search(r'(\d{2}\s*(?:inch|cm|-inch))\b', title_lower)
        cat_specs["Size"] = sz_m.group(1) if sz_m else specs.get('size', 'Cabin / Check-in Size')
        mat_m = re.search(r'\b(polycarbonate|polyester|leather|nylon|abs|hard luggage|soft luggage)\b', title_lower)
        cat_specs["Material"] = mat_m.group(1).title() if mat_m else specs.get('material', 'Durable Polycarbonate / Polyester')
        cat_specs["Lock Type"] = "TSA Lock" if 'tsa' in title_lower else ("Combination Lock" if 'lock' in title_lower else "Not Available")
        cat_specs["Wheels"] = "4 Wheel 360° Spinner" if any(w in title_lower for w in ['4 wheel', 'spinner', '360', 'wheel']) else "Not Available"
    elif c_norm in ("kitchen", "home_appliance"):
        cat_specs["Product Type"] = "Mixer Grinder" if ('mixer' in title_lower or 'grinder' in title_lower) else ("Cooker" if 'cooker' in title_lower else ("Kettle" if 'kettle' in title_lower else "Kitchen Appliance"))
        w_m = re.search(r'\b(\d{3,4})\s*(?:w|watt|watts)\b', title_lower)
        cat_specs["Wattage"] = f"{w_m.group(1)} W" if w_m else specs.get('wattage', 'Not Available')
        cap_m = re.search(r'(\d+(?:\.\d+)?)\s*(?:l|ltr|litres?|liters?)\b', title_lower)
        cat_specs["Capacity"] = f"{cap_m.group(1)} Litres" if cap_m else specs.get('capacity', 'Not Available')
        jar_m = re.search(r'(\d+)\s*jars?\b', title_lower)
        cat_specs["Jars Count"] = f"{jar_m.group(1)} Jars" if jar_m else "Not Available"
        mat_m = re.search(r'\b(stainless steel|steel|copper|non-stick|cast iron|aluminum)\b', title_lower)
        cat_specs["Material"] = mat_m.group(1).title() if mat_m else specs.get('material', 'Stainless Steel / Food Grade')
    elif c_norm in ("headphones", "earphones"):
        cat_specs["Headphone Type"] = "True Wireless (TWS)" if any(w in title_lower for w in ['tws', 'earbuds', 'buds']) else ("Over Ear" if 'over' in title_lower else "In-Ear Neckband")
        bt_m = re.search(r'\b(bluetooth\s*\d(?:\.\d)?|v\d(?:\.\d)?)\b', title_lower)
        cat_specs["Connectivity"] = bt_m.group(0).title() if bt_m else "Bluetooth Wireless"
        pb_m = re.search(r'(\d{2,3})\s*(?:hours?|hrs?)\b', title_lower)
        cat_specs["Playback Time"] = f"{pb_m.group(1)} Hours" if pb_m else "Not Available"
        cat_specs["Noise Cancellation"] = "Active Noise Cancellation (ANC)" if 'anc' in title_lower else ("Environmental Noise Cancellation (ENC)" if 'enc' in title_lower else "Not Available")
        cat_specs["Mic"] = "Built-in Mic" if 'mic' in title_lower else "Not Available"
    elif c_norm in ("watch", "smartwatch", "smartwatches"):
        cat_specs["Display Size"] = specs.get('display', 'HD Smart Display')
        cat_specs["Calling Support"] = "Bluetooth Calling" if ('calling' in title_lower or 'bluetooth' in title_lower) else "Smart Notifications"
        cat_specs["Health Sensors"] = "Heart Rate, SpO2, Step Counter"
        cat_specs["Water Resistance"] = "IP68 Water Resistant" if 'ip68' in title_lower else "Water Resistant"
    else:
        cat_specs["Material"] = specs.get('material', 'Not Available')
        cat_specs["Color"] = specs.get('color', 'Not Available')
        cat_specs["Weight"] = specs.get('weight', 'Not Available')
        cat_specs["Wattage"] = specs.get('wattage', 'Not Available')

    specs['category_specs'] = cat_specs
    return specs


def enrich_products_with_specs(products: list, query: str = "") -> list:
    """
    Attach extracted specs to every product in the list.
    """
    for item in products:
        if 'specs' not in item or 'category_specs' not in item.get('specs', {}):
            item['specs'] = extract_specs(item, query)
    return products


# ---------------------------------------------------------------------------
# Section 4 & 5: Normalization & Category Profiles
# ---------------------------------------------------------------------------

KEY_MAP = {
    # Quantity / Pack
    "net_quantity": "Net Quantity",
    "net quantity (n)": "Net Quantity",
    "net qty": "Net Quantity",
    "pack_quantity": "Pack Quantity",
    "pack of": "Pack Quantity",
    "pack size": "Pack Quantity",
    "number_of_items": "Pack Quantity",
    "quantity": "Quantity",
    
    # Weight / Volume
    "item_weight": "Weight",
    "net_weight": "Weight",
    "weight": "Weight",
    "item weight": "Weight",
    "volume": "Volume",
    "capacity": "Capacity",
    
    # Color / Material / Fabric
    "colour": "Color",
    "color_name": "Color",
    "color": "Color",
    "fabric_type": "Fabric",
    "fabric": "Fabric",
    "material": "Material",
    "material_type": "Material",
    "sole_material": "Sole Material",
    "outer_material": "Material",
    
    # Ratings / Reviews
    "avg_rating": "Rating",
    "average_rating": "Rating",
    "rating": "Rating",
    "review_count": "Total Reviews",
    "reviews": "Total Reviews",
    "total_reviews": "Total Reviews",
    
    # Product identification
    "product_name": "Product Name",
    "title": "Product Name",
    "brand_name": "Brand",
    "brand": "Brand",
    "model_name": "Model",
    "model_number": "Model",
    "model": "Model",
    "product_type": "Product Type",
    "type": "Product Type",
    "seller_name": "Seller",
    "supplier_name": "Seller",
    "seller": "Seller",
    "supplier": "Seller",
    
    # Electronics
    "ram_capacity": "RAM",
    "system_ram": "RAM",
    "ram": "RAM",
    "storage_capacity": "Storage",
    "internal_storage": "Storage",
    "rom": "Storage",
    "ssd_capacity": "SSD/HDD",
    "processor_type": "Processor",
    "processor_brand": "Processor",
    "processor": "Processor",
    "operating_system": "Operating System",
    "os": "Operating System",
    "screen_size": "Screen Size",
    "display_size": "Screen Size",
    "display": "Display",
    "primary_camera": "Rear Camera",
    "rear_camera": "Rear Camera",
    "secondary_camera": "Front Camera",
    "front_camera": "Front Camera",
    "battery_capacity": "Battery",
    "battery": "Battery",
    "graphics_processor": "Graphics",
    "graphics": "Graphics",
    
    # Beauty & Personal Care
    "fragrance": "Fragrance",
    "scent": "Fragrance",
    "skin_type": "Skin Type",
    "ingredients": "Ingredients",
    "composition": "Ingredients",
    "benefits": "Benefits",
    "spf": "SPF",
    "form": "Form",
    "suitable_for": "Suitable For",
    
    # Clothing & Footwear
    "size": "Size",
    "pattern": "Pattern",
    "fit": "Fit",
    "fit_type": "Fit",
    "sleeve": "Sleeve",
    "sleeve_length": "Sleeve",
    "neck": "Neck",
    "neck_style": "Neck",
    "occasion": "Occasion",
    "wash_care": "Wash Care",
    "closure": "Closure",
    "fastening": "Closure",
    
    # Grocery & Food
    "flavor": "Flavor",
    "flavour": "Flavor",
    "diet_type": "Diet Type",
    "country_of_origin": "Country of Origin",
    "shelf_life": "Shelf Life",
    "expiry": "Expiry",
}


def normalize_specification_key(key: str) -> str:
    """
    Standardize different marketplace field names to prevent duplicate rows.
    """
    if not key:
        return ""
    clean = str(key).strip().lower().replace('-', '_').replace('(', '').replace(')', '').strip()
    if clean in KEY_MAP:
        return KEY_MAP[clean]
    
    if 'color' in clean or 'colour' in clean:
        return "Color"
    if 'weight' in clean:
        return "Weight"
    if 'quantity' in clean or 'qty' in clean:
        return "Quantity"
    if 'fabric' in clean:
        return "Fabric"
    if 'rating' in clean:
        return "Rating"
    if 'review' in clean:
        return "Total Reviews"
    if 'processor' in clean:
        return "Processor"
    if 'storage' in clean:
        return "Storage"
    if 'battery' in clean:
        return "Battery"
    if 'camera' in clean:
        return "Camera"
    if 'ingredient' in clean:
        return "Ingredients"
    if 'fragrance' in clean or 'scent' in clean:
        return "Fragrance"
    if 'skin' in clean and 'type' in clean:
        return "Skin Type"
    
    return str(key).strip().title()


CATEGORY_SPEC_PROFILES = {
    "soap": [
        "Product Name", "Brand", "Product Type", "Price", "Rating", "Total Reviews",
        "Weight", "Pack Quantity", "Fragrance", "Skin Type", "Ingredients",
        "Benefits", "Availability", "Seller", "Buy Link"
    ],
    "skincare": [
        "Product Name", "Brand", "Product Type", "Price", "Rating", "Total Reviews",
        "Quantity", "Skin Type", "SPF", "Ingredients", "Benefits", "Form",
        "Fragrance", "Suitable For", "Availability", "Seller", "Buy Link"
    ],
    "face_wash": [
        "Product Name", "Brand", "Product Type", "Price", "Rating", "Total Reviews",
        "Quantity", "Skin Type", "SPF", "Ingredients", "Benefits", "Form",
        "Fragrance", "Suitable For", "Availability", "Seller", "Buy Link"
    ],
    "beauty": [
        "Product Name", "Brand", "Product Type", "Price", "Rating", "Total Reviews",
        "Quantity", "Skin Type", "SPF", "Ingredients", "Benefits", "Form",
        "Fragrance", "Suitable For", "Availability", "Seller", "Buy Link"
    ],
    "phone": [
        "Product Name", "Brand", "Model", "Price", "Rating", "Total Reviews",
        "RAM", "Storage", "Processor", "Display", "Screen Size", "Rear Camera",
        "Front Camera", "Battery", "Operating System", "5G", "Color",
        "Availability", "Seller", "Buy Link"
    ],
    "mobile": [
        "Product Name", "Brand", "Model", "Price", "Rating", "Total Reviews",
        "RAM", "Storage", "Processor", "Display", "Screen Size", "Rear Camera",
        "Front Camera", "Battery", "Operating System", "5G", "Color",
        "Availability", "Seller", "Buy Link"
    ],
    "laptop": [
        "Product Name", "Brand", "Model", "Price", "Rating", "Total Reviews",
        "Processor", "RAM", "Storage", "SSD/HDD", "Display", "Screen Size",
        "Graphics", "Operating System", "Battery", "Weight", "Color",
        "Availability", "Seller", "Buy Link"
    ],
    "clothing": [
        "Product Name", "Brand", "Price", "Rating", "Total Reviews",
        "Fabric", "Material", "Color", "Size", "Pattern", "Fit", "Sleeve",
        "Neck", "Occasion", "Wash Care", "Availability", "Seller", "Buy Link"
    ],
    "shoes": [
        "Product Name", "Brand", "Model", "Price", "Rating", "Total Reviews",
        "Size", "Color", "Material", "Sole Material", "Closure", "Pattern",
        "Occasion", "Availability", "Seller", "Buy Link"
    ],
    "grocery": [
        "Product Name", "Brand", "Price", "Rating", "Total Reviews",
        "Net Quantity", "Weight", "Ingredients", "Flavor", "Diet Type",
        "Country of Origin", "Shelf Life", "Expiry", "Availability", "Seller", "Buy Link"
    ],
    "food": [
        "Product Name", "Brand", "Price", "Rating", "Total Reviews",
        "Net Quantity", "Weight", "Ingredients", "Flavor", "Diet Type",
        "Country of Origin", "Shelf Life", "Expiry", "Availability", "Seller", "Buy Link"
    ],
    "headphones": [
        "Product Name", "Brand", "Model", "Price", "Rating", "Total Reviews",
        "Product Type", "Headphone Type", "Connectivity", "Playback Time",
        "Noise Cancellation", "Mic", "Color", "Availability", "Seller", "Buy Link"
    ],
    "smartwatch": [
        "Product Name", "Brand", "Model", "Price", "Rating", "Total Reviews",
        "Display Size", "Battery Life", "Calling Support", "Water Resistance",
        "Color", "Availability", "Seller", "Buy Link"
    ],
    "default": [
        "Product Name", "Brand", "Model", "Price", "Rating", "Total Reviews",
        "Product Type", "Availability", "Weight", "Color", "Size", "Seller", "Buy Link"
    ]
}


# Common standard fields prioritising Quality of Product first, then Price of Product
_COMMON_IDENTITY_FIELDS = ["Product Name", "Brand", "Model", "Product Type"]

_COMMON_QUALITY_FIELDS = [
    "Quality Score", "Quality Band", "Data Confidence", "Build Material",
    "Warranty", "Seller", "Rating", "Reviews", "Quality Highlights"
]

_COMMON_PRICE_FIELDS = [
    "Price", "MRP", "Discount", "Value for Money Index"
]

# Category Specification Candidate Profiles
CATEGORY_SPEC_FIELDS: dict[str, list[str]] = {
    # 1. Chargers & Power Adapters
    "charger": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Output Voltage", "Output Wattage", "Power", "Input Voltage", "Connector Type",
            "Port Type", "Compatibility", "Fast Charging", "Cable Included", "Color",
            "Availability", "Buy Link"
        ]
    ),
    "adapter": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Output Voltage", "Output Wattage", "Power", "Input Voltage", "Connector Type",
            "Port Type", "Compatibility", "Fast Charging", "Cable Included", "Color",
            "Availability", "Buy Link"
        ]
    ),
    "power_bank": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Battery Capacity", "Output Wattage", "Output Ports", "Fast Charging",
            "Color", "Weight", "Availability", "Buy Link"
        ]
    ),
    # 2. Smartphones & Mobiles
    "smartphone": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "RAM", "Storage", "Display", "Processor", "Battery", "Camera",
            "Front Camera", "OS", "Color", "SIM", "5G", "Availability", "Buy Link"
        ]
    ),
    "phone": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "RAM", "Storage", "Display", "Processor", "Battery", "Camera",
            "Front Camera", "OS", "Color", "SIM", "5G", "Availability", "Buy Link"
        ]
    ),
    # 3. Laptops & Computers
    "laptop": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Processor", "RAM", "Storage", "SSD/HDD", "Display", "Graphics",
            "OS", "Battery", "Weight", "Color", "Availability", "Buy Link"
        ]
    ),
    "tablet": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Processor", "RAM", "Storage", "Display", "Battery", "Camera",
            "OS", "Connectivity", "Color", "Availability", "Buy Link"
        ]
    ),
    # 4. Grocery & Food
    "grocery": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Weight", "Pack Quantity", "Quantity", "Ingredients", "Flavor",
            "Dietary Preference", "Shelf Life", "Unit Price", "Availability", "Buy Link"
        ]
    ),
    "food": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Weight", "Pack Quantity", "Quantity", "Ingredients", "Flavor",
            "Dietary Preference", "Shelf Life", "Unit Price", "Availability", "Buy Link"
        ]
    ),
    # 5. Soaps & Cleansers
    "soap": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Weight", "Net Quantity", "Pack Quantity", "Skin Type", "Ingredients",
            "Fragrance", "Benefits", "Availability", "Buy Link"
        ]
    ),
    # 6. Face Wash & Skincare
    "face_wash": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Skin Type", "Net Quantity", "Volume", "Ingredients", "Suitable Use",
            "Fragrance", "Benefits", "Availability", "Buy Link"
        ]
    ),
    "skincare": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Skin Type", "Net Quantity", "Volume", "Ingredients", "Suitable Use",
            "Fragrance", "Benefits", "Availability", "Buy Link"
        ]
    ),
    "beauty": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Skin Type", "Net Quantity", "Volume", "Weight", "Ingredients",
            "Suitable Use", "Fragrance", "Benefits", "Availability", "Buy Link"
        ]
    ),
    # 7. Clothing & Fashion
    "clothing": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Size", "Color", "Material", "Fabric", "Pattern", "Fit",
            "Sleeve", "Occasion", "Quantity", "Availability", "Buy Link"
        ]
    ),
    "fashion": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Size", "Color", "Material", "Fabric", "Pattern", "Fit",
            "Sleeve", "Occasion", "Quantity", "Availability", "Buy Link"
        ]
    ),
    # 8. Audio & Headphones
    "headphones": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Headphone Type", "Connectivity", "Battery Life", "Noise Cancellation",
            "Driver Size", "Microphone", "Color", "Availability", "Buy Link"
        ]
    ),
    "earphones": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Headphone Type", "Connectivity", "Battery Life", "Noise Cancellation",
            "Driver Size", "Microphone", "Color", "Availability", "Buy Link"
        ]
    ),
    # 9. Watches
    "watch": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Display", "Dial Shape", "Strap Material", "Water Resistance",
            "Battery Life", "Connectivity", "Color", "Availability", "Buy Link"
        ]
    ),
    # 10. Bags & Luggage
    "bags": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Material", "Bag Type", "Capacity", "Compartments", "Closure",
            "Dimensions", "Color", "Weight", "Pack Quantity", "Availability", "Buy Link"
        ]
    ),
    # 11. Kitchen Appliances & Cookware
    "kitchen": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Wattage", "Capacity", "Jars Count", "Material", "Color",
            "Speed Settings", "Motor Type", "Availability", "Buy Link"
        ]
    ),
    "home_appliance": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Capacity", "Wattage", "Energy Rating", "Material", "Color",
            "Availability", "Buy Link"
        ]
    ),
    # 12. Default / General
    "default": (
        _COMMON_IDENTITY_FIELDS + _COMMON_QUALITY_FIELDS + _COMMON_PRICE_FIELDS + [
            "Color", "Material", "Quantity", "Weight", "Dimensions",
            "Availability", "Buy Link"
        ]
    )
}

# Compatibility alias for existing callers
CATEGORY_SPEC_PROFILES = CATEGORY_SPEC_FIELDS

# Strict category blacklist to prevent unrelated specifications from polluting tables
CATEGORY_BLACKLISTED_FIELDS: dict[str, set[str]] = {
    "charger": {
        "ram", "storage", "rom", "processor", "cpu", "camera", "front camera",
        "rear camera", "display", "screen size", "os", "operating system", "graphics",
        "gpu", "sim", "5g", "skin type", "ingredients", "fragrance", "fabric",
        "sleeve", "fit", "sole material", "toe shape", "flavor"
    },
    "adapter": {
        "ram", "storage", "rom", "processor", "cpu", "camera", "front camera",
        "rear camera", "display", "screen size", "os", "operating system", "graphics",
        "gpu", "sim", "5g", "skin type", "ingredients", "fragrance", "fabric",
        "sleeve", "fit", "sole material", "toe shape", "flavor"
    },
    "soap": {
        "ram", "storage", "rom", "processor", "cpu", "camera", "display", "os",
        "operating system", "wattage", "voltage", "connector type", "ports",
        "graphics", "sim", "5g", "fabric", "sleeve", "fit", "battery life"
    },
    "face_wash": {
        "ram", "storage", "rom", "processor", "cpu", "camera", "display", "os",
        "operating system", "wattage", "voltage", "connector type", "ports",
        "graphics", "sim", "5g", "fabric", "sleeve", "fit", "battery life"
    },
    "skincare": {
        "ram", "storage", "rom", "processor", "cpu", "camera", "display", "os",
        "operating system", "wattage", "voltage", "connector type", "ports",
        "graphics", "sim", "5g", "fabric", "sleeve", "fit", "battery life"
    },
    "beauty": {
        "ram", "storage", "rom", "processor", "cpu", "camera", "display", "os",
        "operating system", "wattage", "voltage", "connector type", "ports",
        "graphics", "sim", "5g", "fabric", "sleeve", "fit", "battery life"
    },
    "grocery": {
        "ram", "storage", "rom", "processor", "cpu", "camera", "display", "os",
        "operating system", "wattage", "voltage", "connector type", "ports",
        "graphics", "sim", "5g", "fabric", "sleeve", "fit", "battery life"
    },
    "food": {
        "ram", "storage", "rom", "processor", "cpu", "camera", "display", "os",
        "operating system", "wattage", "voltage", "connector type", "ports",
        "graphics", "sim", "5g", "fabric", "sleeve", "fit", "battery life"
    },
    "clothing": {
        "ram", "storage", "rom", "processor", "cpu", "camera", "display", "os",
        "operating system", "wattage", "voltage", "connector type", "ports",
        "graphics", "sim", "5g", "skin type", "ingredients", "battery life"
    },
    "fashion": {
        "ram", "storage", "rom", "processor", "cpu", "camera", "display", "os",
        "operating system", "wattage", "voltage", "connector type", "ports",
        "graphics", "sim", "5g", "skin type", "ingredients", "battery life"
    },
    "phone": {
        "output voltage", "output wattage", "input voltage", "connector type",
        "fabric", "sleeve", "fit", "skin type", "ingredients", "fragrance",
        "shelf life", "dietary preference"
    },
    "smartphone": {
        "output voltage", "output wattage", "input voltage", "connector type",
        "fabric", "sleeve", "fit", "skin type", "ingredients", "fragrance",
        "shelf life", "dietary preference"
    },
    "laptop": {
        "output voltage", "input voltage", "fabric", "sleeve", "fit",
        "skin type", "ingredients", "fragrance", "shelf life", "dietary preference"
    }
}


def has_real_value(value: Any) -> bool:
    """
    Checks if a specification value contains genuine extracted product information.
    Rejects missing/null data, empty strings, and scraping error or fallback placeholders.
    """
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        invalid_values = {
            "", "n/a", "na", "not available", "unknown", "null", "none",
            "scraping unavailable", "no matching product", "no matching product found",
            "scraper unavailable", "temporarily unavailable", "—", "-", "generic",
            "0", "0.0", "₹0", "rs. 0", "rs 0", "0 rating", "0 reviews", "0%"
        }
        cleaned = value.strip()
        if not cleaned or cleaned.lower() in invalid_values:
            return False
        low = cleaned.lower()
        if "temporarily unavailable" in low or "scraping unavailable" in low or "no matching product" in low:
            return False
        return True
    return True


def should_display_row(amazon_value: Any, flipkart_value: Any, meesho_value: Any, gec_value: Any = None) -> bool:
    """
    Determines whether a specification row should be rendered.
    Returns True only if at least one marketplace has a genuine extracted value.
    """
    return any([
        has_real_value(amazon_value),
        has_real_value(flipkart_value),
        has_real_value(meesho_value),
        has_real_value(gec_value)
    ])


def is_valid_image_url(url: Any) -> bool:
    """Validates that an image URL is a well-formed web URL and not a placeholder."""
    if not url or not isinstance(url, str):
        return False
    u = url.strip()
    if not (u.startswith("http://") or u.startswith("https://") or u.startswith("data:image/")):
        return False
    if len(u) < 10:
        return False
    if any(ph in u.lower() for ph in ("placeholder", "no_image", "not_available", "none", "null")):
        return False
    return True


def get_fields_for_category(category: str) -> list[str]:
    """Returns prioritized candidate specification fields tailored for the detected category."""
    cat_clean = (category or "").strip().lower()
    fields = None
    if cat_clean in CATEGORY_SPEC_FIELDS:
        fields = list(CATEGORY_SPEC_FIELDS[cat_clean])
    else:
        for k, f_list in CATEGORY_SPEC_FIELDS.items():
            if k in cat_clean or cat_clean in k:
                fields = list(f_list)
                break
    if not fields:
        fields = list(CATEGORY_SPEC_FIELDS["default"])
    if "Buy Link" not in fields:
        fields.append("Buy Link")
    return fields


def get_category_spec_profile(category: str) -> list:
    """Backward compatible alias returning candidate fields for a category."""
    return get_fields_for_category(category)


def get_relevant_specifications(category: str, specifications: Optional[dict] = None) -> list[str]:
    """Helper alias returning relevant spec keys for a category."""
    return get_fields_for_category(category)


def extract_field_value(product: Optional[dict], field: str, category: str) -> Optional[str]:
    """
    Extracts an authentic specification value from a marketplace product object.
    Never invents fake values (₹0, 0 rating, Generic brand, etc.).
    Returns None if missing or invalid.
    """
    if not product or not isinstance(product, dict):
        return None

    cat_low = (category or "").strip().lower()
    f_clean = field.strip()
    f_low = f_clean.lower()

    # Reject blacklisted fields for this product category
    blacklist = CATEGORY_BLACKLISTED_FIELDS.get(cat_low, set())
    if f_low in blacklist:
        return None

    # 1. Product Name / Title
    if f_low in ("product name", "title", "name"):
        title = product.get("title") or product.get("product_name")
        return str(title).strip() if has_real_value(title) else None

    # 2. Brand
    if f_low == "brand":
        b = product.get("brand")
        if has_real_value(b) and str(b).strip().lower() not in ("generic", "unknown", "other"):
            return str(b).strip()
        return None

    # 3. Model
    if f_low in ("model", "model number", "model name"):
        m = product.get("model") or product.get("model_number")
        if has_real_value(m) and str(m).strip().lower() not in ("standard", "generic", "n/a", "unknown"):
            return str(m).strip()
        return None

    # 4. Product Type
    if f_low in ("product type", "category"):
        title_l = str(product.get("title") or "").lower()
        if any(w in title_l for w in ("backpack", "laptop bag", "sleeve", "sleeve case", "duffel", "luggage", "trolley")):
            if "backpack" in title_l:
                return "Laptop Backpack"
            if "sleeve" in title_l:
                return "Laptop Sleeve"
            return "Laptop Bag"
        pt = product.get("product_type") or product.get("category")
        if has_real_value(pt) and str(pt).strip().lower() not in ("other", "general", "default"):
            return str(pt).strip().title()
        return None

    # Quality Score (First Preference)
    if f_low in ("quality score", "estimated quality", "estimated quality score", "quality"):
        qs = product.get("quality_score") or (product.get("quality_report", {}).get("raw_quality_score") if isinstance(product.get("quality_report"), dict) else None)
        q_band = product.get("quality_band") or (product.get("quality_report", {}).get("quality_band") if isinstance(product.get("quality_report"), dict) else "Standard Quality")
        if qs is not None:
            return f"{float(qs):.0f}/100 ({q_band})"
        return None

    # Quality Band
    if f_low in ("quality band", "quality rating"):
        q_band = product.get("quality_band") or (product.get("quality_report", {}).get("quality_band") if isinstance(product.get("quality_report"), dict) else None)
        if has_real_value(q_band):
            return str(q_band).strip()
        return None

    # Data Confidence
    if f_low in ("data confidence", "evidence confidence", "evidence completeness"):
        dc = product.get("data_confidence") or (product.get("quality_report", {}).get("data_confidence") if isinstance(product.get("quality_report"), dict) else None)
        if dc is not None:
            return f"{float(dc):.0f}% Verified Evidence"
        return None

    # Build Material
    if f_low in ("build material", "material", "fabric", "build"):
        q_mat = (product.get("quality_attributes", {}).get("material") if isinstance(product.get("quality_attributes"), dict) else None)
        mat = q_mat or product.get("material") or product.get("fabric")
        if has_real_value(mat):
            return str(mat).strip().title()
        return None

    # Quality Highlights / Review Sentiment
    if f_low in ("quality highlights", "review insights", "positive themes", "key strengths"):
        themes = product.get("positive_themes") or (product.get("quality_report", {}).get("review_signals", {}).get("positive_themes") if isinstance(product.get("quality_report"), dict) else [])
        if themes and isinstance(themes, list):
            return " • ".join(str(t) for t in themes[:2])
        return None

    # Value for Money Index
    if f_low in ("value for money", "value for money index", "value index"):
        vfm = product.get("vfm_index")
        if vfm is not None and float(vfm) > 0:
            return f"{float(vfm):.1f}/100"
        qs = product.get("quality_score")
        p_num = product.get("price_num")
        if qs and p_num and p_num > 0:
            from search.quality_scorer import calculate_value_for_money_index
            calc_vfm = calculate_value_for_money_index(float(qs), float(p_num), float(p_num))
            return f"{calc_vfm:.1f}/100"
        return None

    # 5. Price
    if f_low == "price":
        p_num = product.get("price_num")
        p_str = product.get("price")
        if p_num and p_num > 0:
            return f"₹{int(p_num):,}" if p_num == int(p_num) else f"₹{p_num:,.2f}"
        if has_real_value(p_str) and str(p_str).strip() not in ("0", "₹0", "₹ 0"):
            return str(p_str).strip()
        return None

    # 6. MRP / Original Price
    if f_low in ("mrp", "original price"):
        m_num = product.get("mrp_num")
        m_str = product.get("mrp") or product.get("original_price")
        if m_num and m_num > 0:
            return f"₹{int(m_num):,}" if m_num == int(m_num) else f"₹{m_num:,.2f}"
        if has_real_value(m_str) and str(m_str).strip() not in ("0", "₹0", "₹ 0"):
            return str(m_str).strip()
        return None

    # 7. Discount
    if f_low in ("discount", "discount %"):
        d = product.get("discount") or product.get("discount_percent")
        if has_real_value(d) and str(d).strip().lower() not in ("0", "0%", "none"):
            d_str = str(d).strip()
            return d_str if "%" in d_str else f"{d_str}% off"
        return None

    # 8. Rating
    if f_low == "rating":
        r = product.get("rating")
        if r is not None:
            try:
                rf = float(str(r).replace("★", "").strip())
                if rf > 0:
                    return f"★ {rf:.1f}" if rf != int(rf) else f"★ {int(rf)}"
            except Exception:
                pass
        return None

    # 9. Reviews / Review Count
    if f_low in ("reviews", "total reviews", "review count"):
        rc = product.get("review_count") or product.get("reviews")
        if rc is not None:
            try:
                clean_rc = re.sub(r"[^\d]", "", str(rc))
                if clean_rc and int(clean_rc) > 0:
                    return f"{int(clean_rc):,} reviews"
            except Exception:
                pass
        return None

    # 10. Availability
    if f_low == "availability":
        avail = product.get("availability")
        if has_real_value(avail):
            return str(avail).strip()
        if product.get("in_stock") is True:
            return "In Stock"
        elif product.get("in_stock") is False:
            return "Out of Stock"
        return None

    # 11. Buy Link
    if f_low in ("buy link", "link", "url", "product url"):
        lnk = product.get("product_url") or product.get("link") or product.get("url")
        if has_real_value(lnk):
            return str(lnk).strip()
        return None

    # 11. Seller
    if f_low == "seller":
        seller = product.get("seller")
        if has_real_value(seller) and str(seller).strip().lower() not in ("verified seller", "unknown"):
            return str(seller).strip()
        return None

    # 12. Warranty
    if f_low == "warranty":
        w = product.get("warranty")
        if has_real_value(w):
            return str(w).strip()

    # 13. Color
    if f_low == "color":
        c = product.get("color")
        if has_real_value(c):
            return str(c).strip().capitalize()

    # 14. Size
    if f_low == "size":
        s = product.get("size")
        if has_real_value(s):
            return str(s).strip().upper()

    # 15. Material / Fabric
    if f_low in ("material", "fabric"):
        mat = product.get("material") or product.get("fabric")
        if has_real_value(mat):
            return str(mat).strip().capitalize()

    # 16. Weight
    if f_low == "weight":
        wt = product.get("weight")
        if has_real_value(wt):
            return str(wt).strip()

    # 17. Quantity / Pack Quantity
    if f_low in ("quantity", "net quantity", "pack quantity", "pack size"):
        qty = product.get("quantity") or product.get("net_quantity") or product.get("pack_quantity") or product.get("pack_size")
        if has_real_value(qty):
            q_str = str(qty).strip()
            if q_str.isdigit():
                return f"Pack of {q_str}" if int(q_str) > 1 else "Pack of 1"
            return q_str

    # 18. Volume
    if f_low in ("volume", "net volume"):
        vol = product.get("volume") or product.get("net_volume")
        if has_real_value(vol):
            return str(vol).strip()

    # 19. Check explicit specifications dictionary
    specs_dicts = [
        product.get("specifications"),
        product.get("specs"),
        product.get("attributes"),
        product.get("specs", {}).get("category_specs") if isinstance(product.get("specs"), dict) else None
    ]
    for sp_dict in specs_dicts:
        if isinstance(sp_dict, dict):
            for sk, sv in sp_dict.items():
                if sk.strip().lower() == f_low and has_real_value(sv):
                    return str(sv).strip()
            for sk, sv in sp_dict.items():
                sk_clean = sk.strip().lower()
                if (f_low in sk_clean or sk_clean in f_low) and has_real_value(sv):
                    return str(sv).strip()

    # 20. Title-based fallback strictly guarded by detected category
    title = str(product.get("title") or "").strip()
    title_low = title.lower()

    if cat_low in ("charger", "adapter"):
        if f_low in ("output wattage", "wattage", "power"):
            m = re.search(r'\b(\d{1,3})\s*(?:w|watt|watts)\b', title_low)
            if m:
                return f"{m.group(1)}W"
        elif f_low in ("output voltage", "voltage"):
            m = re.search(r'\b(\d{1,3}(?:\.\d+)?)\s*v\b', title_low)
            if m:
                return f"{m.group(1)}V"
        elif f_low in ("input voltage",):
            m = re.search(r'\b(100[-–\s]*240\s*v(?:ac)?)\b', title_low)
            if m:
                return m.group(1).upper()
        elif f_low in ("connector type", "port type"):
            if "type c" in title_low or "type-c" in title_low or "usb-c" in title_low:
                return "Type-C"
            if "blue pin" in title_low:
                return "Blue Pin (4.5mm)"
            m = re.search(r'(\d(?:\.\d)?\s*mm\s*[x*]\s*\d(?:\.\d)?\s*mm)', title_low)
            if m:
                return m.group(1)
        elif f_low == "compatibility":
            for b_name in ("hp", "dell", "lenovo", "asus", "acer", "apple", "samsung", "realme", "vivo"):
                m = re.search(rf'for\s+({b_name}[\w\s]+?)(?:laptop|charger|adapter|\d+w|,|\.|$)', title_low)
                if m:
                    return m.group(1).strip().title()
        elif f_low == "fast charging":
            if any(k in title_low for k in ("fast charge", "fast charging", "quick charge", "dash charge", "warp charge", "vooc")):
                return "Yes (Fast Charging)"

    elif cat_low in ("phone", "smartphone", "mobile"):
        if f_low == "ram":
            m = re.search(r'\b(\d+)\s*gb\s*ram\b', title_low)
            if m:
                return f"{m.group(1)} GB"
        elif f_low in ("storage", "rom"):
            m = re.search(r'\b(64|128|256|512)\s*gb\b|\b(1|2)\s*tb\b', title_low)
            if m:
                return m.group(0).upper()
        elif f_low == "5g":
            if "5g" in title_low:
                return "Yes (5G)"

    elif cat_low == "laptop":
        if f_low == "processor":
            m = re.search(r'\b(core\s*i[3579]|ryzen\s*\d|apple\s*m\d|m[1234])\b', title_low)
            if m:
                return m.group(0).title()
        elif f_low == "ram":
            m = re.search(r'\b(\d+)\s*gb\s*ram\b|\b(\d+)\s*gb\b', title_low)
            if m:
                return f"{m.group(1) or m.group(2)} GB"
        elif f_low in ("storage", "ssd/hdd"):
            m = re.search(r'\b(\d+)\s*(?:gb|tb)\s*(?:ssd|hdd)\b', title_low)
            if m:
                return m.group(0).upper()

    elif cat_low in ("grocery", "food", "soap", "face_wash", "skincare", "beauty"):
        if f_low in ("weight",):
            m = re.search(r'\b(\d+(?:\.\d+)?)\s*(kg|g|gm|grams?)\b', title_low)
            if m:
                return f"{m.group(1)}{m.group(2)}"
        elif f_low in ("volume", "net quantity"):
            m = re.search(r'\b(\d+(?:\.\d+)?)\s*(ml|l|litre?s?|liter?s?)\b', title_low)
            if m:
                return f"{m.group(1)} {m.group(2)}"
        elif f_low in ("ingredients", "active ingredient"):
            v_m = re.search(r'\b(vitamin c|salicylic acid|tea tree|neem|aloe vera|charcoal|hyaluronic|papaya|coffee|ubtan|glycolic)\b', title_low)
            if v_m:
                return v_m.group(1).title()

    elif cat_low in ("kitchen", "home_appliance"):
        if f_low in ("wattage", "power"):
            m = re.search(r'\b(\d{3,4})\s*(?:w|watt|watts)\b', title_low)
            if m:
                return f"{m.group(1)} W"
        elif f_low in ("capacity",):
            m = re.search(r'(\d+(?:\.\d+)?)\s*(?:l|ltr|litres?|liters?)\b', title_low)
            if m:
                return f"{m.group(1)} Litres"
        elif f_low in ("jars count", "jars"):
            m = re.search(r'(\d+)\s*jars?\b', title_low)
            if m:
                return f"{m.group(1)} Jars"
        elif f_low in ("material",):
            m = re.search(r'\b(stainless steel|steel|copper|non-stick|cast iron|aluminum)\b', title_low)
            if m:
                return m.group(1).title()

    elif cat_low in ("bags", "luggage", "backpack"):
        if f_low in ("capacity",):
            m = re.search(r'(\d{2,3})\s*(?:l|ltr|litres?|liters?)\b', title_low)
            if m:
                return f"{m.group(1)} Litres"
        elif f_low in ("dimensions", "size"):
            m = re.search(r'(\d{2}\s*(?:inch|cm|-inch))\b', title_low)
            if m:
                return m.group(1)
        elif f_low in ("material",):
            m = re.search(r'\b(polycarbonate|polyester|leather|nylon|abs)\b', title_low)
            if m:
                return m.group(1).title()

    return None


def build_dynamic_specifications(products: dict, category: str) -> list[dict]:
    """
    Constructs the dynamic specification comparison list.
    - Only includes candidate fields relevant to the detected category.
    - Evaluates Amazon, Flipkart, and Meesho independently.
    - Discards rows where all 3 marketplaces have no valid extracted data.
    - Preserves extracted marketplace data independently without copying.
    - Never displays scraping errors inside table cells; missing cells display '—'.
    """
    candidate_fields = get_fields_for_category(category)

    # Normalize product lookup dictionary keys
    norm_products = {
        "amazon": products.get("amazon") or products.get("Amazon"),
        "flipkart": products.get("flipkart") or products.get("Flipkart"),
        "meesho": products.get("meesho") or products.get("Meesho"),
        "gec": products.get("gec") or products.get("GEC") or products.get("gececom") or products.get("GecEcom"),
    }

    # Also collect any extra genuine specification keys present in product dictionaries
    all_candidate_keys = list(candidate_fields)
    cat_low = (category or "").strip().lower()
    blacklist = CATEGORY_BLACKLISTED_FIELDS.get(cat_low, set())

    for plat_prod in norm_products.values():
        if isinstance(plat_prod, dict):
            specs_dict = plat_prod.get("specifications") or {}
            if isinstance(specs_dict, dict):
                for k, v in specs_dict.items():
                    k_str = str(k).strip()
                    if k_str and k_str.lower() not in blacklist and k_str not in all_candidate_keys:
                        if has_real_value(v):
                            all_candidate_keys.append(k_str)

    rows = []
    for field in all_candidate_keys:
        val_amz = extract_field_value(norm_products["amazon"], field, category)
        val_fk = extract_field_value(norm_products["flipkart"], field, category)
        val_mee = extract_field_value(norm_products["meesho"], field, category)
        val_gec = extract_field_value(norm_products["gec"], field, category)

        if not should_display_row(val_amz, val_fk, val_mee, val_gec):
            continue

        rows.append({
            "field": field,
            "specification": field,
            "name": field,
            "amazon": val_amz if has_real_value(val_amz) else "—",
            "flipkart": val_fk if has_real_value(val_fk) else "—",
            "meesho": val_mee if has_real_value(val_mee) else "—",
            "gec": val_gec if has_real_value(val_gec) else "—"
        })

    return rows


def compute_marketplace_statuses(
    best_match_per_platform: dict,
    platform_status: dict
) -> dict[str, dict]:
    """
    Computes distinct, isolated status badges for each marketplace:
    Status is one of: 'Found', 'Partial', 'Timed Out', 'Unavailable'.
    """
    summary = {}
    for plat in ("Amazon", "Flipkart", "Meesho", "GEC"):
        p_lower = plat.lower()
        prod = best_match_per_platform.get(plat) or best_match_per_platform.get(p_lower)
        p_stat = platform_status.get(plat) or platform_status.get(p_lower) or {}
        stat_code = str(p_stat.get("status", "")).lower()
        is_avail = p_stat.get("available", True)

        if stat_code in ("timeout", "timed_out"):
            summary[plat] = {
                "status": "Timed Out",
                "label": "Timed Out",
                "badge_class": "bg-warning text-dark",
                "color": "warning",
                "icon": "fas fa-clock"
            }
        elif prod and stat_code != "partial" and (prod.get("match_status") in ("EXACT_MATCH", "VARIANT_MATCH") or stat_code == "success" or (prod.get("title") and prod.get("price_num"))):
            summary[plat] = {
                "status": "Found",
                "label": "Found",
                "badge_class": "bg-success text-white",
                "color": "success",
                "icon": "fas fa-check-circle"
            }
        elif prod or stat_code == "partial":
            summary[plat] = {
                "status": "Partial",
                "label": "Partial",
                "badge_class": "bg-info text-dark",
                "color": "info",
                "icon": "fas fa-info-circle"
            }
        elif stat_code in ("blocked", "scraper_error", "unavailable", "access_blocked", "captcha") or not is_avail:
            summary[plat] = {
                "status": "Unavailable",
                "label": "Unavailable",
                "badge_class": "bg-danger text-white",
                "color": "danger",
                "icon": "fas fa-exclamation-triangle"
            }
        else:
            summary[plat] = {
                "status": "Unavailable",
                "label": "Unavailable",
                "badge_class": "bg-secondary text-white",
                "color": "secondary",
                "icon": "fas fa-minus-circle"
            }
    return summary


def build_category_spec_matrix(
    category: str,
    platform_matched_products: dict,
    platform_status: dict
) -> list[dict]:
    """
    Constructs the dynamic category-specific specification comparison matrix.
    Ensures:
    - Display only actual extracted marketplace data.
    - Hide rows when all three marketplaces have no data.
    - Never display scraping errors inside table cells; missing cells display '—'.
    """
    return build_dynamic_specifications(platform_matched_products, category)


def extract_meesho_specs(product: dict) -> dict:
    """
    Inspects all available sources from a Meesho product:
    1. structured product attributes
    2. product details
    3. full_details
    4. description
    5. known product fields
    6. highlights/features
    Merges them into a standardized specifications dict without fake values.
    """
    if not isinstance(product, dict):
        return {}

    title = product.get('title', '')
    brand = product.get('brand') or 'N/A'
    if brand == 'Generic':
        brand = 'N/A'

    price = product.get('price') or (f"₹{product.get('price_num'):,}" if product.get('price_num') else "N/A")
    rating = str(product.get('rating') or 'N/A')
    if rating not in ('N/A', '', 'None') and not rating.startswith('★'):
        rating = f"★ {rating}"

    reviews = str(product.get('reviews') or product.get('review_count') or 'N/A')
    if reviews == '0':
        reviews = 'N/A'

    # Collect all sources of attributes
    raw_attrs = {}
    
    # Source 1: attributes
    if isinstance(product.get('attributes'), dict):
        raw_attrs.update(product['attributes'])
    # Source 2: product_details
    if isinstance(product.get('product_details'), dict):
        raw_attrs.update(product['product_details'])
    # Source 3: full_details
    if isinstance(product.get('full_details'), dict):
        raw_attrs.update(product['full_details'])
    # Source 4: raw_data extracted attributes
    if isinstance(product.get('raw_data'), dict) and isinstance(product['raw_data'].get('extracted_attributes'), dict):
        raw_attrs.update(product['raw_data']['extracted_attributes'])

    # Normalize raw attribute keys
    norm_attrs = {}
    for k, v in raw_attrs.items():
        if v and str(v).strip() and str(v).strip().lower() not in ('none', 'null', 'n/a'):
            norm_k = normalize_specification_key(k)
            norm_attrs[norm_k] = str(v).strip()

    # Base specifications
    specs = {
        "Product Name": title or "N/A",
        "Brand": brand,
        "Model": product.get('model') or norm_attrs.get('Model', 'N/A'),
        "Price": price,
        "Rating": rating,
        "Total Reviews": reviews,
        "Product Type": product.get('product_type') or norm_attrs.get('Product Type', 'N/A'),
        "Availability": product.get('availability', 'In Stock'),
        "Seller": product.get('seller') or product.get('supplier') or norm_attrs.get('Seller', 'N/A'),
        "Buy Link": product.get('product_url') or product.get('link') or "https://www.meesho.com",
    }

    # Weight
    wt = product.get('weight') or norm_attrs.get('Weight')
    if not wt or wt == 'N/A':
        wt_m = re.search(r'\b(\d+(?:\.\d+)?)\s*(g|gm|grams?|kg|kilograms?)\b', title, re.I)
        wt = f"{wt_m.group(1)} {wt_m.group(2).lower()}" if wt_m else "N/A"
    specs["Weight"] = wt

    # Pack Quantity
    pq = product.get('pack_quantity') or product.get('quantity') or norm_attrs.get('Pack Quantity') or norm_attrs.get('Net Quantity')
    if not pq or pq == 'N/A':
        pq_m = re.search(r'(?:pack\s*of\s*|pack-)(\d+)', title, re.I)
        pq = pq_m.group(1) if pq_m else "1"
    specs["Pack Quantity"] = str(pq)
    specs["Quantity"] = str(pq)
    specs["Net Quantity"] = str(pq)

    # Size & Color & Material & Fabric
    specs["Size"] = product.get('size') or norm_attrs.get('Size', 'N/A')
    specs["Color"] = product.get('color') or norm_attrs.get('Color', 'N/A')
    specs["Material"] = product.get('material') or norm_attrs.get('Material', 'N/A')
    specs["Fabric"] = product.get('fabric') or norm_attrs.get('Fabric') or specs["Material"]

    # Fragrance & Scent
    fg = norm_attrs.get('Fragrance')
    if not fg or fg == 'N/A':
        fg_m = re.search(r'\b(sandalwood|saffron|kesar|rose|lavender|neem|aloe|charcoal|lemon|jasmine|tulsi)\b', title, re.I)
        fg = fg_m.group(1).title() if fg_m else "N/A"
    specs["Fragrance"] = fg

    # Skin Type
    st = norm_attrs.get('Skin Type')
    if not st or st == 'N/A':
        if 'all skin' in title.lower() or 'all skin' in product.get('description', '').lower():
            st = "All Skin Types"
        elif 'oily' in title.lower():
            st = "Oily Skin"
        elif 'dry' in title.lower():
            st = "Dry Skin"
        else:
            st = "N/A"
    specs["Skin Type"] = st

    # Ingredients
    ing = norm_attrs.get('Ingredients')
    if not ing or ing == 'N/A':
        ing_parts = []
        for word in ['saffron', 'sandalwood', 'glycerin', 'kojic acid', 'vitamin c', 'tea tree', 'neem', 'aloe vera', 'haldi', 'turmeric']:
            if word in title.lower() or word in product.get('description', '').lower():
                ing_parts.append(word.title())
        ing = ", ".join(ing_parts) if ing_parts else "N/A"
    specs["Ingredients"] = ing

    # Benefits
    ben = norm_attrs.get('Benefits')
    if not ben or ben == 'N/A':
        ben_parts = []
        for word in ['tan reducing', 'de-tan', 'detan', 'glowing', 'brightening', 'dark spot', 'pigmentation', 'moisturizing', 'acne control']:
            if word in title.lower() or word in product.get('description', '').lower():
                ben_parts.append(word.title())
        ben = ", ".join(ben_parts) if ben_parts else "N/A"
    specs["Benefits"] = ben

    # Merge any remaining normalized attributes
    for k, v in norm_attrs.items():
        if k not in specs:
            specs[k] = v

    product["specifications"] = specs
    return specs


