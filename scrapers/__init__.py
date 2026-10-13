"""
scrapers package
================
Provides adapters for Amazon, Flipkart, and Meesho with normalized interfaces.
"""

from scrapers.scraper_result import ScrapeStatus, create_normalized_product
from scrapers.browser_manager import BrowserManager
from scrapers.base_scraper import BaseScraper
from scrapers.amazon_scraper import AmazonScraper
from scrapers.flipkart_scraper import FlipkartScraper
from scrapers.meesho_scraper import MeeshoScraper
from scrapers.gec_scraper import GecScraper

__all__ = [
    "ScrapeStatus",
    "create_normalized_product",
    "BrowserManager",
    "BaseScraper",
    "AmazonScraper",
    "FlipkartScraper",
    "MeeshoScraper",
    "GecScraper",
]
