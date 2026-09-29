🇷🇺 [Читать на русском](README_RU.md)

A high-performance e-commerce intelligence scraper engineered to extract real-time catalog listings, pricing metrics, and inventory data from Ozon while seamlessly bypassing enterprise-grade WAF layers.

### Key Architectural Highlights:
* **WAF Evasion & TLS Spoofing:** Realistic simulation of modern browser TLS/JA4 signatures and HTTP/2 transport frames preventing bot mitigation triggers.
* **Incremental State Merging:** Automatic deduplication and payload aggregation appending fresh catalog listings to existing JSON datasets without overwriting historical records.
* **Environment-Driven Pipelines:** Decoupled execution runtime accepting dynamic target URLs, category filters, and session parameters via `.env`.
* **Resilient Schema Extraction:** Robust extraction pipeline capturing product identifiers, price deltas, review scores, and stock indicators.

### Tech Stack:
* Python 3.12
* curl_cffi (Low-level TLS/JA4 impersonation engine)
* BeautifulSoup4 (High-speed DOM tree extraction)
* python-dotenv (Twelve-Factor environment isolation)

### Quick Start:
```bash
pip install -r requirements.txt
python ozon_parser.py
