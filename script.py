import re
import json
import warnings
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from dateutil.parser import UnknownTimezoneWarning
from icalendar import Calendar, Event

# Suppress the harmless but annoying timezone warning
warnings.filterwarnings("ignore", category=UnknownTimezoneWarning)

# Configuration
BASE_URL = "https://businesshub.southbaldwinchamber.com"
SEARCH_URL = f"{BASE_URL}/calendar/Search"
DETAILS_URL = f"{BASE_URL}/calendar/Details/{{}}"
# Updated to reflect the new 6-month timeframe
OUTPUT_FILE = "calendar_next_6_months.ics"

# Headers to prevent bot blocking
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def get_date_range():
    """Calculates the date range for the next 6 months (approx 180 days)."""
    today = datetime.now()
    future_date = today + timedelta(days=180)
    return today.strftime("%Y-%m-%d"), future_date.strftime("%Y-%m-%d")


def fetch_event_links():
    """Queries the GrowthZone calendar search endpoint and extracts unique event IDs."""
    start_date, end_date = get_date_range()
    params = {"from": start_date, "to": end_date}

    print(f"🔍 Fetching events from {start_date} to {end_date}...")
    try:
        response = requests.get(
            SEARCH_URL, params=params, headers=HEADERS, timeout=15
        )
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"❌ Failed to query calendar search endpoint: {e}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    all_links = soup.find_all("a", href=True)

    seen_ids = set()
    unique_event_ids = []

    for link in all_links:
        href = link["href"]
        if "/calendar/Details/" in href:
            # Grab the numerical ID and strip any URL parameters
            raw_id = href.split("/")[-1]
            event_id = raw_id.split("?")[0]

            if event_id and event_id not in seen_ids:
                seen_ids.add(event_id)
                unique_event_ids.append(event_id)

    print(f"🎯 Found {len(unique_event_ids)} distinct event links on the search page.")
    return unique_event_ids


def parse_event_details(event_id):
    """Visits the individual event page and scrapes all requested fields."""
    url = DETAILS_URL.format(event_id)
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    # --- 1. Extract Title ---
    title_tag = soup.find("h1") or soup.find(class_="gz-event-title")
    title = title_tag.get_text(strip=True) if title_tag else "Chamber Event"

    # Initialize data fields
    start_time = None
    end_time = None
    location = ""
    description = ""

    # --- 2. Extract Location ---
    loc_tag = soup.find(attrs={"itemprop": "location"}) or soup.find(class_=re.compile(r"location", re.I))
    if loc_tag:
        location = loc_tag.get_text(separator=", ", strip=True)
        location = re.sub(r'\s+', ' ', location)
        location = re.sub(r',\s*,', ',', location).strip(", ")

    # --- 3. Extract Dates, Times, & Background Descriptions (JSON-LD) ---
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, list):
                data = data[0]
            if data.get("@type") == "Event":
                if data.get("startDate"):
                    start_time = date_parser.parse(data["startDate"])
                if data.get("endDate"):
                    end_time = date_parser.parse(data["endDate"])
                if data.get("description"):
                    description = data["description"].strip()
                
                # Check JSON for a cleaner location if HTML failed
                if not location and data.get("location"):
                    loc_data = data["location"]
                    if isinstance(loc_data, dict):
                        name = loc_data.get("name", "")
                        addr = loc_data.get("address", "")
                        if isinstance(addr, dict):
                            addr = f"{addr.get('streetAddress', '')}, {addr.get('addressLocality', '')}, {addr.get('addressRegion', '')}".strip(" ,")
                        location = f"{name} {addr}".strip()
                    elif isinstance(loc_data, str):
                        location = loc_data
                break
        except Exception:
            continue

    # --- 4. Visual Fallback for Dates/Times ---
    if not start_time:
        subtitle = soup.find("h5", class_="gz-subtitle")
        if subtitle:
            text = subtitle.get_text(separator=" ", strip=True)
            match_standard = re.search(r'([A-Za-z]+,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4})\s*\(([^)]+)\)', text)
            if match_standard:
                date_str = match_standard.group(1)
                time_str = match_standard.group(2)
                times = time_str.split('-')
                try:
                    start_time = date_parser.parse(f"{date_str} {times[0].strip()}")
                    if len(times) > 1:
                        end_time = date_parser.parse(f"{date_str} {times[1].strip()}")
                except (ValueError, TypeError):
                    pass
            else:
                try:
                    parts = text.split('-')
                    start_time = date_parser.parse(parts[0], fuzzy=True)
                    if len(parts) > 1:
                        end_time = date_parser.parse(parts[1], fuzzy=True)
                except (ValueError, TypeError, OverflowError):
                    pass

    if not start_time:
        date_candidates = soup.find_all(lambda tag: tag.name in ['div', 'span', 'p', 'li'] 
                                        and tag.get('class') 
                                        and any('date' in c.lower() or 'time' in c.lower() for c in tag.get('class')))
        for candidate in date_candidates:
            text = candidate.get_text(separator=" ", strip=True)
            try:
                start_time = date_parser.parse(text, fuzzy=True)
                if start_time.year > 2000:
                    break
            except (ValueError, OverflowError):
                continue

    # --- 5. Visual Fallback for Event Description ---
    if not description:
        # Look for standard GrowthZone description containers
        desc_tag = (soup.find(class_=re.compile(r"gz-event-description|gz-details-description", re.I)) or 
                    soup.find(attrs={"itemprop": "description"}))
        if desc_tag:
            description = desc_tag.get_text(separator="\n", strip=True)

    # --- Final Data Assembly ---
    if not start_time:
        print(f"\n⚠️ Skipping '{title}' (ID: {event_id}) - Absolutely no valid dates found on page.")
        return None

    if not end_time:
        end_time = start_time + timedelta(hours=1)

    return {
        "title": title,
        "start": start_time,
        "end": end_time,
        "location": location if location else "Location not specified",
        "description": description if description else "No additional description provided.",
        "url": url,
        "uid": f"gz-{event_id}@southbaldwinchamber.com",
    }


def generate_ics(events):
    """Builds a Google Calendar compatible .ics file from processed events."""
    cal = Calendar()
    cal.add("prodid", "-//South Baldwin Chamber Scraper//EN")
    cal.add("version", "2.0")

    success_count = 0
    for item in events:
        if not item:
            continue

        event = Event()
        event.add("summary", item["title"])
        event.add("dtstart", item["start"])
        event.add("dtend", item["end"])
        event.add("location", item["location"])
        
        # Merge the parsed text details and the backlink URL into the GCal description box
        full_description = f"{item['description']}\n\n---\n🌐 View original event: {item['url']}"
        event.add("description", full_description)
        
        event.add("uid", item["uid"])
        event.add("dtstamp", datetime.now())

        cal.add_component(event)
        success_count += 1

    with open(OUTPUT_FILE, "wb") as f:
        f.write(cal.to_ical())

    print(f"\n💾 Successfully wrote {success_count} events to file: '{OUTPUT_FILE}'")


def main():
    event_ids = fetch_event_links()
    if not event_ids:
        print("🛑 No events found to process.")
        return

    processed_events = []
    for idx, eid in enumerate(event_ids, 1):
        print(f"⏳ Processing details for event {idx}/{len(event_ids)}...", end="\r")
        evt_data = parse_event_details(eid)
        if evt_data:
            processed_events.append(evt_data)
            
    print(f"\n✅ Finished processing all {len(event_ids)} events!")

    generate_ics(processed_events)
    print("🚀 Script execution finished completely.")


if __name__ == "__main__":
    main()
