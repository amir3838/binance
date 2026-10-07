import os
import re
import time
import json
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

SEED_URL = "https://www.binance.com/ar"
OUTPUT_ROOT = Path(__file__).resolve().parent / "cloned_site"
ALLOWED_DOMAINS = {"www.binance.com", "binance.com"}
SKIP_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css", ".js", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".map", ".pdf", ".mp4", ".mp3", ".webm", ".json"}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


class MirrorError(RuntimeError):
    pass


def normalize_url(raw_url):
    if not raw_url:
        return ""
    value = raw_url.strip()
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("javascript:") or value.startswith("mailto:") or value.startswith("tel:"):
        return ""
    return value


def is_same_domain(url):
    if not url:
        return False
    parsed = urlparse(url)
    netloc = (parsed.netloc or "").lower()
    return netloc in ALLOWED_DOMAINS or netloc.startswith("www.")


def local_file_for_url(url, include_query=False):
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path == "/":
        local = OUTPUT_ROOT / "index.html"
    else:
        clean_path = path.strip("/")
        if clean_path.endswith("/"):
            clean_path = clean_path.rstrip("/") + "/index.html"
        if "." not in Path(clean_path).name:
            clean_path = clean_path + "/index.html"
        local = OUTPUT_ROOT / clean_path

    if include_query and parsed.query:
        local = local.with_name(local.name + "_" + re.sub(r"[^a-zA-Z0-9_]", "_", parsed.query)[:80])
    return local


def ensure_parent(path):
    path.parent.mkdir(parents=True, exist_ok=True)


def save_bytes(path, payload):
    ensure_parent(path)
    path.write_bytes(payload)


def save_text(path, text, encoding="utf-8"):
    ensure_parent(path)
    path.write_text(text, encoding=encoding, errors="ignore")


def safe_relpath(target_path: Path, source_dir: Path):
    rel = os.path.relpath(target_path, start=source_dir).replace("\\", "/")
    if rel.startswith("../"):
        return rel
    if rel == ".":
        return "./"
    return rel if rel.startswith("./") else rel


def url_to_relative_link(source_html_path: Path, target_url: str):
    if not target_url:
        return target_url
    target_url = normalize_url(target_url)
    if not target_url:
        return ""
    parsed = urlparse(target_url)
    if parsed.scheme and parsed.netloc:
        if parsed.netloc.lower() not in ALLOWED_DOMAINS:
            return target_url
        target_local = local_file_for_url(target_url)
        if not target_local.exists():
            return target_url
        rel = os.path.relpath(target_local, start=source_html_path.parent).replace("\\", "/")
        return rel

    base_local = local_file_for_url(urljoin("https://www.binance.com", parsed.path or "/"))
    rel = os.path.relpath(base_local, start=source_html_path.parent).replace("\\", "/")
    return rel


def download_url(url, retries=2):
    normalized = normalize_url(url)
    if not normalized:
        return None
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc.lower() not in ALLOWED_DOMAINS:
        return None
    target_path = local_file_for_url(normalized)
    if target_path.exists():
        return str(target_path)

    try:
        response = requests.get(normalized, timeout=30, headers={"User-Agent": USER_AGENT}, allow_redirects=True)
        response.raise_for_status()
        save_bytes(target_path, response.content)
        return str(target_path)
    except Exception:
        if retries <= 0:
            return None
        time.sleep(1)
        return download_url(url, retries - 1)


def is_page_candidate(url):
    if not url:
        return False
    normal = normalize_url(url)
    if not normal:
        return False
    parsed = urlparse(normal)
    if parsed.netloc.lower() not in ALLOWED_DOMAINS:
        return False
    if parsed.fragment:
        normal = normal.split("#", 1)[0]
    if not parsed.path or parsed.path == "/":
        return True
    ext = Path(unquote(parsed.path)).suffix.lower()
    return ext not in SKIP_EXT


def html_file_for_url(url):
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc not in ALLOWED_DOMAINS:
        return None
    if not parsed.path or parsed.path == "/":
        return OUTPUT_ROOT / "index.html"
    clean_path = parsed.path.strip("/")
    if not clean_path:
        return OUTPUT_ROOT / "index.html"
    if clean_path.endswith("/"):
        clean_path = clean_path.rstrip("/") + "/index.html"
    return OUTPUT_ROOT / clean_path if (Path(clean_path).suffix or clean_path.endswith("/")) else OUTPUT_ROOT / clean_path / "index.html"


def rewrite_css_file(css_path: Path, css_url: str):
    if not css_path.exists():
        return
    try:
        content = css_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return

    def repl(match):
        full = match.group(0)
        url_value = match.group(1)
        url_value = url_value.strip().strip('"\'')
        if not url_value or url_value.startswith("data:") or url_value.startswith("#"):
            return full
        absolute = urljoin(css_url, url_value)
        if not is_same_domain(absolute):
            return full
        downloaded = download_url(absolute)
        if not downloaded:
            return full
        local_target = Path(downloaded)
        rel = os.path.relpath(local_target, start=css_path.parent).replace("\\", "/")
        quote = '"' if '"' in full else "'" if "'" in full else ""
        if quote:
            return f"url({quote}{rel}{quote})"
        return f"url({rel})"

    new_content = re.sub(r"url\((?:\s*)(?:'\"|\")?([^)]*?)(?:\s*['\"])?\s*\)", repl, content, flags=re.I)
    if new_content != content:
        css_path.write_text(new_content, encoding="utf-8", errors="ignore")


def rewrite_resource_urls_for_html(html_path: Path, page_url: str):
    if not html_path.exists():
        return
    try:
        text = html_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup.find_all(True):
        for attr in ["href", "src", "srcset", "poster"]:
            if attr not in tag.attrs:
                continue
            value = tag.get(attr)
            if value is None:
                continue
            if attr == "srcset":
                pairs = []
                for part in value.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    if " " in part:
                        url_part, desc = part.split(None, 1)
                        rewritten = url_to_relative_link(html_path, urljoin(page_url, url_part))
                        pairs.append(f"{rewritten} {desc}")
                    else:
                        rewritten = url_to_relative_link(html_path, urljoin(page_url, part))
                        pairs.append(rewritten)
                tag[attr] = ", ".join(pairs)
                continue
            if attr == "href" and value.startswith("#"):
                continue
            if value.startswith("data:"):
                continue
            target = urljoin(page_url, value)
            if is_same_domain(target):
                local_target = local_file_for_url(target)
                if local_target.exists():
                    try:
                        rel = os.path.relpath(local_target, start=html_path.parent).replace("\\", "/")
                        tag[attr] = rel
                    except Exception:
                        pass
    out = str(soup)
    html_path.write_text(out, encoding="utf-8", errors="ignore")


def walk_site(seed_url):
    queue = deque([seed_url])
    seen = set()
    downloaded_assets = set()
    downloaded_pages = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=r"C:\Users\AMIR\AppData\Local\ms-playwright\chromium-1243\chrome-win64\chrome.exe")
        context = browser.new_context(viewport={"width": 1440, "height": 1400}, user_agent=USER_AGENT)
        page = context.new_page()

        while queue:
            url = queue.popleft()
            normal = normalize_url(url)
            if not normal or normal in seen:
                continue
            seen.add(normal)

            try:
                page.goto(normal, wait_until="networkidle", timeout=120000)
                time.sleep(1.5)
            except Exception:
                try:
                    page.goto(normal, wait_until="domcontentloaded", timeout=120000)
                    time.sleep(1)
                except Exception:
                    continue

            html = page.content()
            out_file = html_file_for_url(normal)
            if out_file is None:
                continue
            save_text(out_file, html, encoding="utf-8")
            downloaded_pages.add(normal)

            soup = BeautifulSoup(html, "html.parser")
            for tag in soup.find_all(True):
                for attr in ["href", "src", "srcset", "poster"]:
                    if attr not in tag.attrs:
                        continue
                    value = tag.get(attr)
                    if not value:
                        continue
                    if value.startswith("data:"):
                        continue
                    if attr == "href" and value.startswith("#"):
                        continue

                    if attr == "srcset":
                        for part in value.split(","):
                            maybe = part.strip().split()[0] if part.strip() else ""
                            if maybe:
                                absolute = urljoin(normal, maybe)
                                if is_same_domain(absolute):
                                    downloaded_assets.add(absolute)
                        continue

                    absolute = urljoin(normal, value)
                    if is_same_domain(absolute):
                        downloaded_assets.add(absolute)

            for link in soup.find_all("a", href=True):
                href = normalize_url(link.get("href"))
                if not href:
                    continue
                href = href.split("#", 1)[0]
                if not href:
                    continue
                absolute = urljoin(normal, href)
                if is_same_domain(absolute) and is_page_candidate(absolute):
                    queue.append(absolute)

        browser.close()

    asset_urls = sorted(downloaded_assets)
    for asset_url in asset_urls:
        if not asset_url:
            continue
        parsed = urlparse(asset_url)
        ext = Path(unquote(parsed.path)).suffix.lower()
        if ext in {".html", ".htm"}:
            continue
        downloaded = download_url(asset_url)
        if not downloaded:
            continue
        local_path = Path(downloaded)
        if local_path.suffix.lower() == ".css":
            rewrite_css_file(local_path, asset_url)

    for page_url in sorted(downloaded_pages):
        page_file = html_file_for_url(page_url)
        if page_file and page_file.exists():
            rewrite_resource_urls_for_html(page_file, page_url)

    return downloaded_pages, sorted(downloaded_assets)


def create_preview_helper():
    root_index = OUTPUT_ROOT / "index.html"
    root_index.write_text(
        "<!doctype html><html><head><meta http-equiv=\"refresh\" content=\"0; url=/ar/index.html\"></head><body><p>Redirecting to <a href=\"/ar/index.html\">/ar/index.html</a></p></body></html>",
        encoding="utf-8",
    )

    bat = OUTPUT_ROOT / "run_server.bat"
    bat.write_text(
        "@echo off\r\n"
        "cd /d \"%~dp0\"\r\n"
        "python -m http.server 8000\r\n",
        encoding="utf-8",
    )

    ps1 = OUTPUT_ROOT / "run_server.ps1"
    ps1.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "Set-Location $PSScriptRoot\n"
        "python -m http.server 8000\n",
        encoding="utf-8",
    )

    return bat, ps1, root_index


def main():
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    pages, assets = walk_site(SEED_URL)
    bat, ps1, root_index = create_preview_helper()
    print(f"Pages found: {len(pages)}")
    print(f"Assets downloaded: {len(assets)}")
    print(f"Mirror saved to: {OUTPUT_ROOT}")
    print(f"Root redirect page: {root_index}")
    print(f"Preview launchers: {bat} and {ps1}")
    print("Preview: python -m http.server 8000 --directory \"C:\\Users\\AMIR\\Documents\\binance\\cloned_site\"")


if __name__ == "__main__":
    main()
