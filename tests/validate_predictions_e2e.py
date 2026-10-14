import os
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.price_prediction_service import predict_single_product_7d, get_marketplace_7d_predictions

products_to_test = [
    ('Apple iPhone 15 (128 GB) - Black', 'Amazon', 54999),
    ('Apple iPhone 15 (128 GB) - Black', 'Flipkart', 53999),
    ('Vivo T4 5G (Starlight Silver, 128 GB)', 'Flipkart', 18999),
    ('Vivo T4 5G', 'GEC E-Commerce', 18499),
    ('Samsung Galaxy Tab A9+ (Wi-Fi, 64 GB)', 'Amazon', 16999),
    ('Samsung Galaxy Tab A9+', 'Flipkart', 16499),
    ('Dell Inspiron 15', 'Amazon', 42990),
    ('boAt Rockerz 450 Bluetooth On Ear Headphones', 'Amazon', 1299),
    ('Sony WH-1000XM5 Wireless Noise Cancelling Headphones', 'Amazon', 29990),
    ('Wildcraft 35L Backpack', 'Amazon', 1899),
    ('Ant Esports GM320 RGB Optical Wired Gaming Mouse', 'Amazon', 699),
    ('Pigeon by Stovekraft 1.5 Litre Stainless Steel Electric Kettle', 'gec', 494),
    ('BrandNewQuantumWidgetXYZ_Unknown', 'Amazon', 999)
]

print('=== Testing Single Product 7-Day Predictions ===')
for p_title, plat, curr_p in products_to_test:
    res = predict_single_product_7d(p_title, platform=plat, current_price=curr_p)
    status = 'PREDICTION OK' if res.get('has_prediction') else 'INSUFFICIENT (HONEST)'
    pts = res.get('data_points_count', 0)
    p7 = res.get('predicted_7d_price')
    slope = res.get('daily_rate_rs')
    msg = res.get('message') or res.get('trend_label') or ''
    print(f'[{status}] {p_title[:38]:<38} ({plat:<14}): {pts:>2} pts | 7d_pred={str(p7):<8} | slope={str(slope):<6}/d | {msg[:45]}')

print('\n=== Testing Marketplace Aggregator ===')
for q in ['iPhone 15', 'Vivo T4', 'Samsung Galaxy Tab', 'Dell Inspiron 15', 'boAt Rockerz 450']:
    agg = get_marketplace_7d_predictions(q)
    active = [p for p, d in agg['predictions'].items() if d.get('has_prediction')]
    print(f'Query "{q:<18}": has_any={agg.get("has_any_prediction")} | active_marketplaces={active} | chart_datasets={len(agg["chart"]["datasets"])}')
