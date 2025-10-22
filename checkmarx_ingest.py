import requests
from bs4 import BeautifulSoup
import yaml
import chromadb
from chromadb.utils import embedding_functions
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit
import re
import time
import hashlib
import random

# --------------------------- CHROMADB SETUP ---------------------------

client = chromadb.PersistentClient(path="./chroma_checkmarx")
embedding_fn = embedding_functions.DefaultEmbeddingFunction()
collection = client.get_or_create_collection(
    name="checkmarx_apis",
    embedding_function=embedding_fn
)

# --------------------------- HTML CRAWLER ---------------------------

class CheckmarxHTMLCrawler:
    def __init__(self, collection):
        self.visited = set()
        self.collection = collection
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) Firefox/123.0"
        ]
        # Regex to match release notes / changelog / version pages
        self.release_notes_pattern = re.compile(
            r"\/34965-\d{5,}-.*(?:release|changelog|version).*\.html", re.IGNORECASE
        )

    def normalize_url(self, url):
        """Remove URL fragment so same page with #section is treated as same."""
        parts = urlsplit(url)
        normalized = urlunsplit(parts._replace(fragment=""))
        return normalized

    def crawl(self, base_url, url=None, depth=0, max_depth=2):
        if url is None:
            url = base_url

        normalized_url = self.normalize_url(url)

        if normalized_url in self.visited or depth > max_depth:
            return True

        # Skip pages matching release notes pattern
        if self.release_notes_pattern.search(normalized_url):
            print(f"⏩ Skipping release notes / changelog URL: {normalized_url}")
            self.visited.add(normalized_url)
            return True

        # Skip exact known release notes pages
        exact_skipped = [
            "https://docs.checkmarx.com/en/34965-280009-single-tenant-current.html",
            "https://docs.checkmarx.com/en/34965-281369-multi-tenant-current.html",
            "https://docs.checkmarx.com/en/34965-314914-previous-single-tenant-releases.html",
            "https://docs.checkmarx.com/en/34965-203785-previous-multi-tenant-releases.html",
            "https://docs.checkmarx.com/en/34965-332351-cli-and-plugin-changelogs.html"
        ]
        if normalized_url in exact_skipped:
            print(f"⏩ Skipping exact release notes URL: {normalized_url}")
            self.visited.add(normalized_url)
            return True

        self.visited.add(normalized_url)

        headers = {
            "User-Agent": random.choice(self.user_agents),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        }

        print(f"[HTML] Crawling (depth {depth}): {normalized_url}")

        try:
            start_time = time.time()
            resp = requests.get(normalized_url, headers=headers, timeout=15)
            elapsed = round(time.time() - start_time, 2)
            print(f"    ↳ Status: {resp.status_code}, Time: {elapsed}s")

            if resp.status_code == 403:
                print(f"🚫 403 Forbidden at {normalized_url}")
                return False
            if not resp.ok:
                print(f"⚠️ Skipping non-OK status: {resp.status_code}")
                return True

            soup = BeautifulSoup(resp.text, "html.parser")
            text = soup.get_text(separator="\n", strip=True)
            if len(text) < 50:
                print("    ⚠️ Skipping empty/short content.")
                return True

            doc_id = hashlib.md5(normalized_url.encode()).hexdigest()
            self.collection.add(
                ids=[doc_id],
                documents=[text],
                metadatas=[{"source": "html", "url": normalized_url}]
            )
            print(f"    ✅ Stored content ({len(text)} chars)")

            # Crawl internal links
            links = soup.find_all("a", href=True)
            for a in links:
                href = a["href"]
                full_url = urljoin(normalized_url, href)
                full_url = self.normalize_url(full_url)
                if urlparse(base_url).netloc in full_url and full_url not in self.visited:
                    time.sleep(0.5)
                    self.crawl(base_url, full_url, depth + 1, max_depth)

        except requests.exceptions.Timeout:
            print(f"⏱️ Timeout on {normalized_url}")
        except Exception as e:
            print(f"❌ Error on {normalized_url}: {e}")

        return True

# --------------------------- YAML CRAWLER ---------------------------

class CheckmarxYAMLCrawler:
    def __init__(self, base_host, yaml_paths, collection):
        self.base_host = base_host.rstrip("/")
        self.yaml_paths = yaml_paths
        self.collection = collection

    def parse_and_store(self):
        success_count = 0
        for item in self.yaml_paths:
            url = urljoin(self.base_host, item["url"])
            name = item["name"]
            print(f"[YAML] Fetching: {url}")
            try:
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                content = yaml.safe_load(resp.text)

                if not isinstance(content, dict) or "paths" not in content:
                    print(f"⚠️ Skipping invalid YAML structure: {url}")
                    continue

                for path, methods in content.get("paths", {}).items():
                    if not isinstance(methods, dict):
                        continue
                    for method, details in methods.items():
                        if not isinstance(details, dict):
                            continue
                        summary = details.get("summary", "")
                        description = details.get("description", "")
                        doc_text = f"{method.upper()} {path}\n{summary}\n{description}"

                        doc_id = hashlib.md5(f"{url}-{method}-{path}".encode()).hexdigest()
                        self.collection.add(
                            ids=[doc_id],
                            documents=[doc_text],
                            metadatas=[{
                                "source": "yaml",
                                "service": name,
                                "method": method,
                                "path": path,
                                "url": url
                            }]
                        )
                        success_count += 1

            except Exception as e:
                print(f"❌ Failed to parse {url}: {e}")
                continue

        print(f"\n✅ YAML ingestion complete: {success_count} endpoints stored.")


# --------------------------- MAIN SCRIPT ---------------------------

if __name__ == "__main__":
    html_crawler = CheckmarxHTMLCrawler(collection=collection)

    html_targets = [
        {"url": "https://docs.checkmarx.com/en/34965-67042-checkmarx-one.html", "depth": 2},
        {"url": "https://checkmarx.stoplight.io/", "depth": 4}
    ]

    html_success = True
    for target in html_targets:
        print(f"\n🌐 Starting HTML crawl for {target['url']} (max depth {target['depth']})")
        success = html_crawler.crawl(target["url"], max_depth=target["depth"])
        if not success:
            html_success = False

    if not html_success:
        user_input = input(
            "\n⚠️ Some HTML pages failed (403 or errors). Continue with YAML crawl? (y/n): "
        ).strip().lower()
        if user_input != "y":
            print("🚫 Aborting YAML crawl. Exiting.")
            exit()

    yaml_urls = [
        {"name": "Access Management", "url": "/spec/v1/frankfurt-core-access-management-ACCESS_MANAGEMENT.yaml"},
        {"name": "Analytics Api", "url": "/spec/v1/frankfurt-metrics-data-analytics-api-ANALYTICS_API.yaml"},
        {"name": "Applications", "url": "/spec/v1/frankfurt-core-scans-APPLICATIONS.yaml"},
        {"name": "Applications", "url": "/spec/v1/frankfurt-scans-scans-APPLICATIONS.yaml"},
        {"name": "Applications Overview", "url": "/spec/v1/frankfurt-core-results-overview-APPLICATIONS_OVERVIEW.yaml"},
        {"name": "Audit Trail", "url": "/spec/v1/frankfurt-audit-trail-api-AUDIT_TRAIL.yaml"},
        {"name": "Best Fix Location", "url": "/spec/v1/frankfurt-sast-results-writer-BEST_FIX_LOCATION.yaml"},
        {"name": "Cloud Insights", "url": "/spec/v1/frankfurt-cnas-cnas-manager-CLOUD_INSIGHTS.yaml"},
        {"name": "Configuration", "url": "/spec/v1/frankfurt-core-configuration-CONFIGURATION.yaml"},
        {"name": "Projects", "url": "/spec/v1/frankfurt-core-scans-PROJECTS.yaml"},
        {"name": "Scans", "url": "/spec/v1/frankfurt-core-scans-SCANS.yaml"},
        {"name": "Sast Results", "url": "/spec/v1/frankfurt-sast-results-writer-SAST_RESULTS.yaml"},
        {"name": "Uploads", "url": "/spec/v1/frankfurt-core-uploads-UPLOADS.yaml"},
        {"name": "Webhooks", "url": "/spec/v1/frankfurt-core-events-WEBHOOKS.yaml"}
    ]

    yaml_crawler = CheckmarxYAMLCrawler(
        base_host="https://deu.ast.checkmarx.net",
        yaml_paths=yaml_urls,
        collection=collection
    )

    yaml_crawler.parse_and_store()
    print("\n✅ Ingestion completed successfully!")
