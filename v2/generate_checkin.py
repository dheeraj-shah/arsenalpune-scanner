#!/usr/bin/env python3
"""Generate check-in HTML for a screening event.

Usage:
    python generate_checkin.py                          # defaults to latest match
    python generate_checkin.py --slug 2026_03_22_carabao_cup_final_arsenal_v_mancity
"""
import json, os, sqlite3, argparse, hashlib, subprocess, shutil

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "data", "arsenal.db")
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "scanner_template.html")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")


def _normalize_phone(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) >= 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits[1:]
    if len(digits) == 10:
        return digits
    return ""


def _normalize_name(name: str) -> str:
    n = name.lower().strip()
    for title in ("mr.", "mrs.", "ms.", "dr.", "shri."):
        if n.startswith(title):
            n = n[len(title):].strip()
    return "".join(n.split())


def get_attendees(conn, slug):
    """Get all attendees for a match slug, with member status and deduplication."""
    members_slug = slug + "_members"

    # Load membership data
    member_emails = set()
    member_names = set()
    phone_to_email = {}

    cur = conn.execute("SELECT name, email, phone FROM payments")
    for row in cur:
        member_emails.add(row[1].lower())
        member_names.add(_normalize_name(row[0]))
        ph = _normalize_phone(row[2])
        if ph:
            phone_to_email[ph] = row[1].lower()

    # Also build phone->email from all screening_payments for indirect matching
    cur = conn.execute("SELECT email, phone FROM screening_payments")
    for row in cur:
        ph = _normalize_phone(row[1])
        if ph and ph not in phone_to_email:
            phone_to_email[ph] = row[0].lower()

    # Build screening history: how many past events each person has attended
    # Count distinct base slugs (strip _members suffix) per email
    cur = conn.execute(
        """SELECT LOWER(email), COUNT(DISTINCT REPLACE(match_name, '_members', ''))
           FROM screening_payments
           GROUP BY LOWER(email)"""
    )
    screening_history = dict(cur.fetchall())

    # Get all screening payments for both slugs
    cur = conn.execute(
        """SELECT id, match_name, name, email, phone, amount, ticket_count, msg_id
           FROM screening_payments
           WHERE match_name IN (?, ?)
           ORDER BY name""",
        (slug, members_slug),
    )

    rows = cur.fetchall()

    # Dedupe by email: if someone is in both general + members, merge
    seen_emails = {}  # email -> attendee dict
    attendees = []

    for row in rows:
        _, match_name, name, email, phone, amount, ticket_count, msg_id = row
        email_lower = email.lower()
        is_members_page = match_name == members_slug

        # Check membership
        is_member = False
        if email_lower in member_emails:
            is_member = True
        elif _normalize_name(name) in member_names:
            is_member = True
        else:
            ph = _normalize_phone(phone)
            if ph:
                mapped_email = phone_to_email.get(ph, "")
                if mapped_email in member_emails:
                    is_member = True

        if email_lower in seen_emails:
            # Already have this person from general list - they're in both
            existing = seen_emails[email_lower]
            if is_members_page:
                # Members RSVP is Rs 1, don't add ticket count (they already have general tickets)
                # Just ensure they're marked as member
                existing["is_member"] = True
            else:
                # This is the general entry, add tickets
                existing["ticket_count"] += ticket_count
            continue

        # status: "paid_member", "paid_guest"
        if is_member or is_members_page:
            status = "paid_member"
        else:
            status = "paid_guest"

        entry = {
            "msg_id": msg_id,
            "name": name,
            "email": email,
            "phone": phone,
            "amount": amount,
            "ticket_count": ticket_count,
            "is_member": is_member or is_members_page,
            "is_members_page": is_members_page,
            "status": status,
            "screenings": screening_history.get(email_lower, 1),
        }
        seen_emails[email_lower] = entry
        attendees.append(entry)

    # Add non-paid members (in payments but not in screening_payments for this match)
    paid_emails = set(seen_emails.keys())
    cur = conn.execute("SELECT name, email, phone FROM payments ORDER BY name")
    nonpaid_count = 0
    for row in cur:
        name, email, phone = row
        if email.lower() not in paid_emails:
            # Generate a stable pseudo msg_id for non-paid members
            pseudo_id = "np_" + hashlib.md5(email.lower().encode()).hexdigest()[:12]
            entry = {
                "msg_id": pseudo_id,
                "name": name,
                "email": email,
                "phone": phone,
                "amount": 0,
                "ticket_count": 1,
                "is_member": True,
                "is_members_page": False,
                "status": "nonpaid_member",
                "screenings": screening_history.get(email.lower(), 0),
            }
            attendees.append(entry)
            nonpaid_count += 1

    return attendees


def get_match_info(conn, slug):
    """Get match display info from calendar + venue tables."""
    parts = slug.split("_")
    date_str = f"{parts[0]}-{parts[1]}-{parts[2]}"

    # Pull from match_calendar
    cal_row = conn.execute(
        "SELECT competition, home_team, away_team, match_time FROM match_calendar WHERE match_date = ?",
        (date_str,),
    ).fetchone()

    if cal_row:
        competition, home_team, away_team, match_time = cal_row
    else:
        # Fallback: parse from slug
        after_date = "_".join(parts[3:])
        v_pos = after_date.rfind("_v_")
        if v_pos > 0:
            before_v_parts = after_date[:v_pos].split("_")
            home_team = before_v_parts[-1].title()
            competition = " ".join(before_v_parts[:-1]).title()
            away_team = after_date[v_pos + 3:].replace("_", " ").title()
        else:
            competition = after_date.replace("_", " ").title()
            home_team = away_team = ""
        match_time = ""

    # Pull from screening_venues
    venue_row = conn.execute(
        "SELECT venue_name, venue_link FROM screening_venues WHERE match_slug = ?",
        (slug,),
    ).fetchone()

    venue_name = venue_row[0] if venue_row else ""
    venue_link = venue_row[1] if venue_row else ""

    # Screening time = 1 hour before match time
    screening_time = ""
    if match_time:
        try:
            from datetime import datetime, timedelta
            # Parse "10:00 PM IST" -> "9:00 PM"
            time_clean = match_time.replace(" IST", "").strip()
            dt = datetime.strptime(time_clean, "%I:%M %p")
            screening_dt = dt - timedelta(hours=1)
            screening_time = screening_dt.strftime("%-I:%M %p")
        except Exception:
            screening_time = ""

    return {
        "date": date_str,
        "competition": competition,
        "home_team": home_team,
        "away_team": away_team,
        "match_time": match_time,
        "screening_time": screening_time,
        "venue_name": venue_name,
        "venue_link": venue_link,
        "display_title": f"{home_team} vs {away_team}" if home_team else competition,
        "display_subtitle": competition,
    }


def generate_html(attendees, match_info, slug, sync_url=""):
    """Generate check-in HTML from the scanner template."""
    # Build guest data in the format the scanner template expects
    # Key = msg_id, Value = {name, email, phone, amount, quantity}
    # We add is_member for the check-in page to display badges
    guest_data = {}
    total_tickets = 0
    paid_member_count = 0
    paid_guest_count = 0
    nonpaid_member_count = 0

    for a in attendees:
        guest_data[a["msg_id"]] = {
            "name": a["name"],
            "email": a["email"],
            "phone": a["phone"],
            "amount": a["amount"],
            "quantity": a["ticket_count"],
            "is_member": a["is_member"],
            "status": a["status"],
            "screenings": a["screenings"],
        }
        if a["status"] != "nonpaid_member":
            total_tickets += a["ticket_count"]
        if a["status"] == "paid_member":
            paid_member_count += 1
        elif a["status"] == "paid_guest":
            paid_guest_count += 1
        else:
            nonpaid_member_count += 1

    with open(TEMPLATE_PATH) as f:
        html = f.read()

    # Replace guest data placeholder
    html = html.replace("__GUEST_DATA__", json.dumps(guest_data))

    # Customize the header for this specific event
    html = html.replace(
        "<title>Arsenal Pune SC - Match Day Scanner</title>",
        f"<title>Arsenal Pune SC - {match_info['display_title']} Check-in</title>",
    )

    # Replace "MATCH DAY" header and "Scanner" badge
    html = html.replace(
        '<span class="header-title">MATCH DAY</span>',
        f'<span class="header-title">{match_info["display_title"].upper()}</span><span style="font-size:10px;opacity:0.7;letter-spacing:1px;margin-left:8px">{match_info["display_subtitle"].upper()}</span>',
    )
    html = html.replace(
        '<span class="header-badge">Scanner</span>',
        f'<span class="header-badge">Check-in</span>',
    )

    # Add member badge styling + member info to search/pending items
    # Inject custom CSS for member badges
    member_css = """
        /* Status badges */
        .member-badge {
            display: inline-flex;
            align-items: center;
            font-size: 9px;
            font-weight: 700;
            letter-spacing: 1px;
            text-transform: uppercase;
            padding: 2px 6px;
            border-radius: 4px;
            margin-left: 6px;
            vertical-align: middle;
            line-height: 1;
        }
        .pending-name {
            display: flex !important;
            align-items: center;
            flex-wrap: wrap;
            gap: 0;
        }
        .member-badge.paid_member {
            color: #FFD700;
            background: rgba(255, 215, 0, 0.15);
            border: 1px solid rgba(255, 215, 0, 0.3);
        }
        .member-badge.paid_guest {
            color: var(--text-muted);
            background: var(--surface-3);
        }
        .member-badge.nonpaid_member {
            color: var(--text-muted);
            background: transparent;
            border: 1px dashed var(--surface-3);
        }

        /* Non-paid member rows */
        .pending-item.nonpaid {
            opacity: 0.5;
        }
        .pending-divider {
            padding: 10px 10px 6px;
            font-size: 10px;
            font-weight: 600;
            color: var(--text-muted);
            letter-spacing: 2px;
            text-transform: uppercase;
            border-top: 1px solid var(--surface-3);
            margin-top: 8px;
        }

        /* Match info bar */
        .match-info-bar {
            background: var(--surface);
            padding: 10px 16px;
            border-bottom: 1px solid var(--surface-2);
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-size: 12px;
            color: var(--text-dim);
        }
        .match-info-bar .venue {
            display: flex;
            align-items: center;
            gap: 4px;
        }
        .match-info-bar .venue a {
            color: var(--text-dim);
            text-decoration: none;
        }
        .match-info-bar .counts {
            display: flex;
            gap: 10px;
        }
        .match-info-bar .count-item {
            display: flex;
            align-items: center;
            gap: 4px;
        }
        .count-dot {
            width: 6px;
            height: 6px;
            border-radius: 50%;
        }
        .count-dot.paid_member { background: #FFD700; }
        .count-dot.paid_guest { background: var(--text-muted); }
        .count-dot.nonpaid_member { background: var(--text-muted); border: 1px dashed var(--text-muted); border-radius: 50%; }

        /* Check-in layout overrides: compact camera, expand pending list */
        .viewfinder { height: 14vh; }
        .result-panel { min-height: 80px; padding: 14px; }
        .tab-content { max-height: none; }
        .stats-bar .stat { padding: 8px 0; }
        .stats-bar .stat-value { font-size: 24px; }
        .search-wrap { padding: 8px 12px; }
        .search-wrap input { padding: 10px 14px; }

        /* Filter sub-tabs */
        .filter-bar {
            display: flex;
            gap: 6px;
            padding: 8px 12px 4px;
        }
        .filter-btn {
            padding: 4px 12px;
            border-radius: 100px;
            border: 1px solid var(--surface-3);
            background: transparent;
            color: var(--text-muted);
            font-family: 'DM Sans', sans-serif;
            font-size: 11px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.15s;
        }
        .filter-btn.active {
            background: var(--red);
            border-color: var(--red);
            color: white;
        }
        .filter-btn:active { transform: scale(0.95); }
        .filter-btn .filter-count {
            font-weight: 400;
            opacity: 0.7;
            margin-left: 2px;
        }

        /* Sticky search */
        .search-wrap {
            position: sticky;
            top: 0;
            z-index: 15;
        }

        /* Swipe to check-in */
        .pending-item {
            position: relative;
            overflow: hidden;
            touch-action: pan-y;
            background: transparent !important;
            padding: 0 !important;
            border-radius: 8px;
        }
        .swipe-bg {
            position: absolute;
            left: 0;
            top: 0;
            bottom: 0;
            background: var(--green);
            display: flex;
            align-items: center;
            padding-left: 16px;
            font-size: 12px;
            font-weight: 600;
            color: #000;
            letter-spacing: 1px;
            text-transform: uppercase;
            width: 0;
            overflow: hidden;
            transition: none;
            pointer-events: none;
            border-radius: 8px 0 0 8px;
            z-index: 0;
            opacity: 0;
        }
        .pending-item-inner {
            position: relative;
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            background: var(--surface-2);
            padding: 10px 10px;
            border-radius: 8px;
            transition: transform 0.1s ease;
            z-index: 1;
            width: 100%;
        }
        .pending-item-inner .pending-badge {
            margin-top: 2px;
        }
        .pending-item.nonpaid .pending-item-inner {
            background: var(--surface-2);
        }

        /* CSV export */
        .export-btn {
            padding: 8px 20px;
            background: transparent;
            color: var(--text-muted);
            border: 1px solid var(--surface-3);
            border-radius: 8px;
            font-family: 'DM Sans', sans-serif;
            font-size: 12px;
            cursor: pointer;
            transition: all 0.15s;
            margin-right: 8px;
        }
        .export-btn:active {
            background: var(--surface-2);
            color: var(--text-dim);
        }

        /* Live stats tab */
        .stats-panel {
            padding: 20px 16px;
            text-align: center;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap: 10px;
            margin-bottom: 16px;
        }
        .stats-card {
            background: var(--surface-2);
            border-radius: 12px;
            padding: 16px 8px;
        }
        .stats-card-num {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 36px;
            line-height: 1;
        }
        .stats-card-num.green { color: var(--green); }
        .stats-card-num.amber { color: var(--amber); }
        .stats-card-lbl {
            font-size: 9px;
            color: var(--text-dim);
            letter-spacing: 2px;
            text-transform: uppercase;
            margin-top: 2px;
        }
        .stats-progress {
            margin: 12px 0;
        }
        .stats-progress-bar {
            height: 6px;
            background: var(--surface-3);
            border-radius: 3px;
            overflow: hidden;
        }
        .stats-progress-fill {
            height: 100%;
            background: var(--green);
            border-radius: 3px;
            transition: width 0.5s ease;
        }
        .stats-progress-label {
            font-size: 11px;
            color: var(--text-dim);
            margin-top: 4px;
        }
        .stats-breakdown {
            display: flex;
            gap: 12px;
            justify-content: center;
            font-size: 11px;
            color: var(--text-dim);
            margin-top: 12px;
        }
        .stats-walkin-total {
            margin-top: 16px;
            padding: 12px;
            background: var(--surface-2);
            border-radius: 10px;
            font-size: 12px;
            color: var(--text-dim);
        }
        .stats-walkin-total span {
            color: var(--green);
            font-weight: 600;
        }

        /* Screening history badge */
        .screening-count {
            display: inline-flex;
            align-items: center;
            font-size: 8px;
            font-weight: 700;
            color: var(--blue);
            background: var(--blue-bg);
            border: 1px solid rgba(41, 121, 255, 0.3);
            padding: 1px 5px;
            border-radius: 4px;
            margin-left: 4px;
            line-height: 1;
            vertical-align: middle;
        }

        /* Walk-in payment */
        .walkin-pay-btn {
            padding: 4px 10px;
            border: 1px solid var(--green);
            border-radius: 6px;
            background: transparent;
            color: var(--green);
            font-family: 'DM Sans', sans-serif;
            font-size: 10px;
            font-weight: 600;
            cursor: pointer;
            letter-spacing: 0.5px;
            transition: all 0.15s;
            white-space: nowrap;
        }
        .walkin-pay-btn:active {
            background: var(--green-bg);
            transform: scale(0.95);
        }
        .walkin-paid {
            color: var(--green);
            font-size: 10px;
            font-weight: 600;
            white-space: nowrap;
        }
        .walkin-modal {
            position: fixed;
            inset: 0;
            z-index: 200;
            display: flex;
            align-items: center;
            justify-content: center;
            background: rgba(0,0,0,0.8);
            backdrop-filter: blur(4px);
            -webkit-backdrop-filter: blur(4px);
        }
        .walkin-modal-content {
            background: var(--surface);
            border: 1px solid var(--surface-3);
            border-radius: 16px;
            padding: 24px;
            width: 300px;
            text-align: center;
        }
        .walkin-modal-title {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 22px;
            letter-spacing: 2px;
            margin-bottom: 4px;
        }
        .walkin-modal-name {
            font-size: 14px;
            color: var(--text-dim);
            margin-bottom: 16px;
        }
        .walkin-amount-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            margin-bottom: 12px;
        }
        .walkin-amount-btn {
            padding: 12px;
            border: 1px solid var(--surface-3);
            border-radius: 10px;
            background: var(--surface-2);
            color: var(--text);
            font-family: 'Bebas Neue', sans-serif;
            font-size: 20px;
            cursor: pointer;
            transition: all 0.15s;
        }
        .walkin-amount-btn:active {
            background: var(--green-bg);
            border-color: var(--green);
        }
        .walkin-custom-wrap {
            display: flex;
            gap: 8px;
            margin-bottom: 16px;
        }
        .walkin-custom-wrap input {
            flex: 1;
            padding: 10px;
            border: 1px solid var(--surface-3);
            border-radius: 8px;
            background: var(--surface-2);
            color: var(--text);
            font-family: 'DM Sans', sans-serif;
            font-size: 14px;
            outline: none;
            text-align: center;
        }
        .walkin-custom-wrap input:focus {
            border-color: var(--green);
        }
        .walkin-custom-wrap button {
            padding: 10px 16px;
            border: none;
            border-radius: 8px;
            background: var(--green);
            color: #000;
            font-family: 'DM Sans', sans-serif;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
        }
        .walkin-cancel {
            background: none;
            border: none;
            color: var(--text-muted);
            font-family: 'DM Sans', sans-serif;
            font-size: 13px;
            cursor: pointer;
            padding: 8px;
        }
    """
    html = html.replace("</style>", member_css + "\n    </style>")

    # Add match info bar after stats bar
    venue_html = match_info["venue_name"]
    if match_info["venue_link"]:
        venue_html = f'<a href="{match_info["venue_link"]}" target="_blank">{match_info["venue_name"]} ↗</a>'

    screening_time = match_info.get("screening_time", "")
    time_html = f'<span style="margin-left:8px">🕘 {screening_time}</span>' if screening_time else ""

    match_info_bar = f"""
    <div class="match-info-bar">
        <div class="venue">📍 {venue_html}{time_html}</div>
        <div class="counts">
            <div class="count-item"><span class="count-dot paid_member"></span> {paid_member_count} paid</div>
            <div class="count-item"><span class="count-dot paid_guest"></span> {paid_guest_count} guests</div>
            <div class="count-item"><span class="count-dot nonpaid_member"></span> {nonpaid_member_count} unpaid</div>
        </div>
    </div>"""
    html = html.replace(
        '</div>\n\n    <div class="viewfinder"',
        f'</div>\n{match_info_bar}\n\n    <div class="viewfinder"',
    )

    # Add Live Stats tab button
    html = html.replace(
        """<button class="tab-btn" onclick="switchTab('log')">Scan Log</button>""",
        """<button class="tab-btn" onclick="switchTab('log')">Scan Log</button>
            <button class="tab-btn" onclick="switchTab('stats')">Live Stats</button>""",
    )

    # Add filter sub-tabs inside both pending and scan log tabs
    html = html.replace(
        '<div class="tab-content active" id="tab-pending"></div>',
        """<div class="tab-content active" id="tab-pending">
            <div class="filter-bar" id="filter-bar-pending">
                <button class="filter-btn active" data-filter="all" onclick="window.setFilter('all')">All</button>
                <button class="filter-btn" data-filter="paid_member" onclick="window.setFilter('paid_member')">Paid Members</button>
                <button class="filter-btn" data-filter="paid_guest" onclick="window.setFilter('paid_guest')">Paid Guests</button>
                <button class="filter-btn" data-filter="nonpaid_member" onclick="window.setFilter('nonpaid_member')">Non Paid</button>
            </div>
            <div id="pending-list"></div>
        </div>""",
    )
    html = html.replace(
        '<div class="tab-content" id="tab-log"></div>',
        """<div class="tab-content" id="tab-log">
            <div class="filter-bar" id="filter-bar-log">
                <button class="filter-btn active" data-filter="all" onclick="window.setFilter('all')">All</button>
                <button class="filter-btn" data-filter="paid_member" onclick="window.setFilter('paid_member')">Paid Members</button>
                <button class="filter-btn" data-filter="paid_guest" onclick="window.setFilter('paid_guest')">Paid Guests</button>
                <button class="filter-btn" data-filter="nonpaid_member" onclick="window.setFilter('nonpaid_member')">Non Paid</button>
            </div>
            <div id="log-list"></div>
        </div>
        <div class="tab-content" id="tab-stats">
            <div class="stats-panel">
                <div class="stats-grid">
                    <div class="stats-card"><div class="stats-card-num" id="stats-total">0</div><div class="stats-card-lbl">Tickets</div></div>
                    <div class="stats-card"><div class="stats-card-num green" id="stats-checked">0</div><div class="stats-card-lbl">Checked In</div></div>
                    <div class="stats-card"><div class="stats-card-num amber" id="stats-remaining">0</div><div class="stats-card-lbl">Remaining</div></div>
                </div>
                <div class="stats-progress">
                    <div class="stats-progress-bar"><div class="stats-progress-fill" id="stats-progress"></div></div>
                    <div class="stats-progress-label" id="stats-pct">0% checked in</div>
                </div>
                <div class="stats-breakdown">
                    <span>🟡 """ + str(paid_member_count) + """ paid members</span>
                    <span>⚪ """ + str(paid_guest_count) + """ paid guests</span>
                </div>
                <div class="stats-walkin-total" id="stats-walkin">No walk-in payments yet</div>
            </div>
        </div>""",
    )

    # Patch the JS to show member badges in search results and pending list
    # Replace search result rendering to include member badge
    html = html.replace(
        """return `<div class="search-item" data-id="${m.id}">
                <div><div class="search-item-name">${m.guest.name}</div><div class="search-item-meta">${m.guest.quantity} ticket${m.guest.quantity > 1 ? 's' : ''}</div></div>
                <span class="search-item-status ${statusClass}">${statusText}</span>
            </div>`;""",
        """const badgeLabels = {paid_member: 'Paid Member', paid_guest: 'Paid Guest', nonpaid_member: 'Non Paid'};
            const memberBadge = '<span class="member-badge ' + m.guest.status + '">' + badgeLabels[m.guest.status] + '</span>';
            const scrCount = m.guest.screenings > 1 ? '<span class="screening-count">' + m.guest.screenings + 'x</span>' : '';
            const amt = m.guest.amount > 0 ? ' · ₹' + Math.round(m.guest.amount) : '';
            return `<div class="search-item" data-id="${m.id}">
                <div><div class="search-item-name">${m.guest.name}${memberBadge}${scrCount}</div><div class="search-item-meta">${m.guest.quantity} ticket${m.guest.quantity > 1 ? 's' : ''}${amt}</div></div>
                <span class="search-item-status ${statusClass}">${statusText}</span>
            </div>`;""",
    )

    # Replace pending list rendering to include member badge
    html = html.replace(
        """return `<div class="pending-item" onclick="window.showResult('${p.id}', 'manual')">
                <div><div class="pending-name">${p.name}</div><div class="pending-tickets">${p.total} ticket${p.total > 1 ? 's' : ''}</div></div>
                <span class="pending-badge">${badge}</span>
            </div>`;""",
        """const guest = GUESTS[p.id];
            const badgeLabels = {paid_member: 'Paid Member', paid_guest: 'Paid Guest', nonpaid_member: 'Non Paid'};
            const st = guest ? guest.status : 'paid_guest';
            const memberBadge = '<span class="member-badge ' + st + '">' + badgeLabels[st] + '</span>';
            const scrCount = guest && guest.screenings > 1 ? '<span class="screening-count">' + guest.screenings + 'x</span>' : '';
            const amt = guest && guest.amount > 0 ? ' · ₹' + Math.round(guest.amount) : '';
            const dimClass = st === 'nonpaid_member' ? ' nonpaid' : '';
            const walkinPaid = window._walkinPayments && window._walkinPayments[p.id];
            const rightSide = st === 'nonpaid_member' && !walkinPaid
                ? '<button class="walkin-pay-btn" onclick="event.stopPropagation();window.showPayModal(\\'' + p.id + '\\')" >₹ Collect</button>'
                : walkinPaid
                    ? '<span class="walkin-paid">₹' + walkinPaid + ' paid</span>'
                    : '<span class="pending-badge">' + badge + '</span>';
            return `<div class="pending-item${dimClass}" data-guest-id="${p.id}" onclick="window.showResult('${p.id}', 'manual')">
                <div class="swipe-bg">CHECK IN</div>
                <div class="pending-item-inner">
                    <div><div class="pending-name">${p.name}${memberBadge}${scrCount}</div><div class="pending-tickets">${p.total} ticket${p.total > 1 ? 's' : ''}${amt}</div></div>
                    ${rightSide}
                </div>
            </div>`;""",
    )

    # Also show member badge in the result panel when checking in
    html = html.replace(
        """resultEl.innerHTML = `<div class="result-icon">&#10003;</div><div class="result-status">WELCOME</div><div class="result-name">${guest.name}</div><div class="result-counter">${record.count}/${max}</div><div class="result-detail">All in</div>`;""",
        """const bl1 = {paid_member: 'Paid Member', paid_guest: 'Paid Guest', nonpaid_member: 'Non Paid'};
            const mb1 = '<span class="member-badge ' + guest.status + '" style="margin-left:0;margin-top:4px">' + bl1[guest.status] + '</span>';
            const amtInfo1 = guest.amount > 0 ? ' · ₹' + Math.round(guest.amount) : '';
            resultEl.innerHTML = `<div class="result-icon">&#10003;</div><div class="result-status">WELCOME</div><div class="result-name">${guest.name}</div>${mb1}<div class="result-counter">${record.count}/${max}</div><div class="result-detail">All in${amtInfo1}</div>`;""",
    )

    html = html.replace(
        """resultEl.innerHTML = `<div class="result-icon">&#10003;</div><div class="result-status">WELCOME</div><div class="result-name">${guest.name}</div><div class="result-counter">${record.count}/${max}</div><div class="result-detail">${max - record.count} more to enter</div>`;""",
        """const bl2 = {paid_member: 'Paid Member', paid_guest: 'Paid Guest', nonpaid_member: 'Non Paid'};
            const mb2 = '<span class="member-badge ' + guest.status + '" style="margin-left:0;margin-top:4px">' + bl2[guest.status] + '</span>';
            const amtInfo2 = guest.amount > 0 ? ' · ₹' + Math.round(guest.amount) : '';
            resultEl.innerHTML = `<div class="result-icon">&#10003;</div><div class="result-status">WELCOME</div><div class="result-name">${guest.name}</div>${mb2}<div class="result-counter">${record.count}/${max}</div><div class="result-detail">${max - record.count} more to enter${amtInfo2}</div>`;""",
    )

    # Patch pendingEl and logEl to target the inner list divs
    html = html.replace(
        "const pendingEl = document.getElementById('tab-pending');",
        "const pendingEl = document.getElementById('pending-list');",
    )
    html = html.replace(
        "const logEl = document.getElementById('tab-log');",
        "const logEl = document.getElementById('log-list');",
    )

    # Inject filter state, setFilter, and log filtering logic
    filter_js = """
    let activeFilter = 'all';
    window.setFilter = function(filter) {
        activeFilter = filter;
        document.querySelectorAll('.filter-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.filter === filter);
        });
        renderPending();
        filterLog();
    };
    function filterLog() {
        document.querySelectorAll('#log-list .log-entry').forEach(entry => {
            if (activeFilter === 'all') {
                entry.style.display = '';
            } else {
                entry.style.display = entry.dataset.status === activeFilter ? '' : 'none';
            }
        });
    }
    """
    html = html.replace(
        "updateStats();\n    renderPending();",
        f"{filter_js}\n    updateStats();\n    renderPending();",
    )

    # Patch switchTab to handle 3 tabs
    html = html.replace(
        """window.switchTab = function(tab) {
        haptic(10);
        document.querySelectorAll('.tab-btn').forEach((btn, i) => {
            btn.classList.toggle('active', (tab === 'pending' && i === 0) || (tab === 'log' && i === 1));
        });
        document.getElementById('tab-pending').classList.toggle('active', tab === 'pending');
        document.getElementById('tab-log').classList.toggle('active', tab === 'log');
    };""",
        """window.switchTab = function(tab) {
        haptic(10);
        document.querySelectorAll('.tab-btn').forEach((btn, i) => {
            btn.classList.toggle('active',
                (tab === 'pending' && i === 0) || (tab === 'log' && i === 1) || (tab === 'stats' && i === 2));
        });
        document.getElementById('tab-pending').classList.toggle('active', tab === 'pending');
        document.getElementById('tab-log').classList.toggle('active', tab === 'log');
        document.getElementById('tab-stats').classList.toggle('active', tab === 'stats');
        if (tab === 'stats') window.refreshStatsTab();
    };""",
    )

    # Patch addLog to store member status on log entries for filtering
    html = html.replace(
        """function addLog(name, time, cls) {
        const entry = document.createElement('div');
        entry.className = `log-entry ${cls}`;
        entry.innerHTML = `<div class="log-dot"></div><div class="log-text">${name}</div><div class="log-time">${time}</div>`;
        logEl.prepend(entry);
    }""",
        """function addLog(name, time, cls, guestId) {
        const entry = document.createElement('div');
        entry.className = `log-entry ${cls}`;
        const guest = guestId ? GUESTS[guestId] : null;
        const st = guest ? guest.status : 'unknown';
        entry.dataset.status = st;
        const logBadgeLabels = {paid_member: 'PM', paid_guest: 'PG', nonpaid_member: 'NP'};
        const mb = guest ? '<span class="member-badge ' + st + '" style="font-size:8px;padding:1px 4px;margin-left:4px">' + (logBadgeLabels[st] || '?') + '</span>' : '';
        entry.innerHTML = `<div class="log-dot"></div><div class="log-text">${name}${mb}</div><div class="log-time">${time}</div>`;
        logEl.prepend(entry);
        filterLog();
    }""",
    )

    # Patch addLog calls to pass guest id
    html = html.replace(
        "addLog(`${guest.name} - ${record.count}/${max}`, time, 'valid');",
        "addLog(`${guest.name} - ${record.count}/${max}`, time, 'valid', id);",
    )
    html = html.replace(
        "addLog(`${guest.name} - ${record.count}/${max}`, time, 'partial');",
        "addLog(`${guest.name} - ${record.count}/${max}`, time, 'partial', id);",
    )
    html = html.replace(
        "addLog(`${guest.name} - already in`, time, 'full');",
        "addLog(`${guest.name} - already in`, time, 'full', id);",
    )
    html = html.replace(
        "addLog(id.substring(0, 12), time, 'invalid');",
        "addLog(id.substring(0, 12), time, 'invalid', id);",
    )

    # Exclude non-paid members from total ticket count in stats
    html = html.replace(
        "totalTickets += guest.quantity;",
        "if (guest.status !== 'nonpaid_member') totalTickets += guest.quantity;",
    )

    # Patch pending sort to group paid first, then non-paid with divider
    html = html.replace(
        "pending.sort((a, b) => a.name.localeCompare(b.name));",
        """pending.sort((a, b) => {
            const aIsPaid = a.status !== 'nonpaid_member' ? 0 : 1;
            const bIsPaid = b.status !== 'nonpaid_member' ? 0 : 1;
            if (aIsPaid !== bIsPaid) return aIsPaid - bIsPaid;
            return a.name.localeCompare(b.name);
        });""",
    )

    # Add divider between paid and non-paid in the rendered output
    html = html.replace(
        "}).join('');",
        """}).join('');\n"""
        """        // Insert divider between paid and non-paid\n"""
        """        if (activeFilter === 'all') {\n"""
        """            const firstNonpaid = pending.findIndex(p => p.status === 'nonpaid_member');\n"""
        """            if (firstNonpaid > 0) {\n"""
        """                const items = pendingEl.querySelectorAll('.pending-item');\n"""
        """                const nonpaidCount = pending.filter(p => p.status === 'nonpaid_member').length;\n"""
        """                if (items[firstNonpaid]) {\n"""
        """                    const divider = document.createElement('div');\n"""
        """                    divider.className = 'pending-divider';\n"""
        """                    divider.textContent = 'Non Paid Members (' + nonpaidCount + ')';\n"""
        """                    items[firstNonpaid].parentNode.insertBefore(divider, items[firstNonpaid]);\n"""
        """                }\n"""
        """            }\n"""
        """        }\n""",
    )

    # Patch renderPending to filter by member/guest
    html = html.replace(
        """if (remaining > 0) {
                pending.push({ id, name: guest.name, total: guest.quantity, remaining });
            }""",
        """if (remaining > 0) {
                const st = guest.status;
                if (activeFilter === 'all' || activeFilter === st) {
                    pending.push({ id, name: guest.name, total: guest.quantity, remaining, status: st });
                }
            }""",
    )

    # Update the localStorage key to be match-specific
    html = html.replace(
        "const scannedKey = 'arsenal_meetup_scanned';",
        f"const scannedKey = 'arsenal_checkin_{slug}';",
    )

    # Add CSV export button to footer
    html = html.replace(
        '<button class="reset-btn" onclick="resetScans()">Reset All Scans</button>',
        '<button class="export-btn" onclick="window.exportCSV()">Export CSV</button>'
        '<button class="reset-btn" onclick="resetScans()">Reset All Scans</button>',
    )

    # Inject swipe-to-check-in, filter counters, and CSV export JS before closing </body>
    extra_js = """
<script>
// --- Filter pill counters ---
function updateFilterCounts() {
    const guests = window.GUESTS || {};
    const scannedData = window.scanned || {};
    const counts = { all: 0, paid_member: 0, paid_guest: 0, nonpaid_member: 0 };
    for (const [id, guest] of Object.entries(guests)) {
        const checkedIn = scannedData[id] ? scannedData[id].count : 0;
        const remaining = guest.quantity - checkedIn;
        if (remaining > 0) {
            counts.all++;
            counts[guest.status]++;
        }
    }
    document.querySelectorAll('.filter-btn').forEach(btn => {
        const f = btn.dataset.filter;
        const count = counts[f];
        if (count !== undefined) {
            const label = btn.textContent.replace(/\\s*\\(\\d+\\)$/, '');
            btn.innerHTML = label + '<span class="filter-count"> (' + count + ')</span>';
        }
    });
}
// Hook into renderPending to update counts
const origRenderPending = window.renderPending || null;
const pendingListEl = document.getElementById('pending-list');
if (pendingListEl) {
    const observer = new MutationObserver(updateFilterCounts);
    observer.observe(pendingListEl, { childList: true, subtree: true });
}
setTimeout(updateFilterCounts, 100);

// --- Swipe to check-in ---
(function() {
    const SWIPE_THRESHOLD = 80;
    let startX = 0, currentX = 0, swiping = null;

    document.addEventListener('touchstart', function(e) {
        const item = e.target.closest('.pending-item');
        if (!item) return;
        startX = e.touches[0].clientX;
        currentX = startX;
        swiping = item;
    }, { passive: true });

    document.addEventListener('touchmove', function(e) {
        if (!swiping) return;
        currentX = e.touches[0].clientX;
        const dx = Math.max(0, currentX - startX);
        const inner = swiping.querySelector('.pending-item-inner');
        const bg = swiping.querySelector('.swipe-bg');
        if (inner && dx > 10) {
            inner.style.transform = 'translateX(' + dx + 'px)';
            inner.style.transition = 'none';
            bg.style.width = dx + 'px';
            bg.style.opacity = '1';
        }
    }, { passive: true });

    document.addEventListener('touchend', function(e) {
        if (!swiping) return;
        const dx = currentX - startX;
        const inner = swiping.querySelector('.pending-item-inner');
        const bg = swiping.querySelector('.swipe-bg');
        const guestId = swiping.dataset.guestId;

        if (dx >= SWIPE_THRESHOLD && guestId) {
            const guest = window.GUESTS[guestId];
            const record = window.scanned[guestId];
            const checkedIn = record ? record.count : 0;
            const remaining = guest ? guest.quantity - checkedIn : 0;
            if (remaining === 1) {
                // Single ticket remaining: auto check-in
                inner.style.transform = 'translateX(100%)';
                inner.style.transition = 'transform 0.2s ease';
                bg.style.width = '100%';
                setTimeout(() => window.checkInGroup(guestId, 1), 200);
            } else if (remaining > 1) {
                // Multiple tickets: show picker
                inner.style.transform = '';
                inner.style.transition = 'transform 0.2s ease';
                bg.style.width = '0';
                bg.style.opacity = '0';
                window.showResult(guestId, 'manual');
            } else {
                inner.style.transform = '';
                inner.style.transition = 'transform 0.2s ease';
                bg.style.width = '0';
                bg.style.opacity = '0';
            }
        } else {
            if (inner) {
                inner.style.transform = '';
                inner.style.transition = 'transform 0.2s ease';
            }
            if (bg) { bg.style.width = '0'; bg.style.opacity = '0'; }
        }
        swiping = null;
    }, { passive: true });
})();

// --- Live Stats Tab ---
window.refreshStatsTab = function() {
    const guests = window.GUESTS || {};
    const scannedData = window.scanned || {};
    let totalTickets = 0, checkedIn = 0;
    for (const [id, guest] of Object.entries(guests)) {
        if (guest.status !== 'nonpaid_member') totalTickets += guest.quantity;
        if (scannedData[id]) checkedIn += scannedData[id].count;
    }
    const remaining = totalTickets - checkedIn;
    const pct = totalTickets > 0 ? Math.round((checkedIn / totalTickets) * 100) : 0;

    const el = (id) => document.getElementById(id);
    if (el('stats-total')) el('stats-total').textContent = totalTickets;
    if (el('stats-checked')) el('stats-checked').textContent = checkedIn;
    if (el('stats-remaining')) el('stats-remaining').textContent = remaining;
    if (el('stats-progress')) el('stats-progress').style.width = pct + '%';
    if (el('stats-pct')) el('stats-pct').textContent = pct + '% checked in';

    // Walk-in payment total
    const walkinPay = window._walkinPayments || {};
    const walkinEntries = Object.entries(walkinPay);
    if (walkinEntries.length > 0) {
        const totalCollected = walkinEntries.reduce((sum, [, amt]) => sum + amt, 0);
        if (el('stats-walkin')) el('stats-walkin').innerHTML = 'Walk-in collections: <span>₹' + totalCollected + '</span> from ' + walkinEntries.length + ' people';
    }
};

// --- Walk-in Payment ---
window._walkinPayments = JSON.parse(localStorage.getItem('walkin_payments') || '{}');

window.showPayModal = function(guestId) {
    const guest = window.GUESTS[guestId];
    if (!guest) return;
    const modal = document.createElement('div');
    modal.className = 'walkin-modal';
    modal.id = 'pay-modal';
    modal.innerHTML = `
        <div class="walkin-modal-content">
            <div class="walkin-modal-title">COLLECT PAYMENT</div>
            <div class="walkin-modal-name">${guest.name}</div>
            <div class="walkin-amount-grid">
                <button class="walkin-amount-btn" onclick="window.recordPayment('${guestId}', 500)">₹500</button>
                <button class="walkin-amount-btn" onclick="window.recordPayment('${guestId}', 1000)">₹1000</button>
                <button class="walkin-amount-btn" onclick="window.recordPayment('${guestId}', 1500)">₹1500</button>
                <button class="walkin-amount-btn" onclick="window.recordPayment('${guestId}', 2000)">₹2000</button>
            </div>
            <div class="walkin-custom-wrap">
                <input type="number" id="custom-amount" placeholder="Custom ₹" inputmode="numeric" />
                <button onclick="const v=document.getElementById('custom-amount').value;if(v>0)window.recordPayment('${guestId}',parseInt(v))">Go</button>
            </div>
            <button class="walkin-cancel" onclick="document.getElementById('pay-modal').remove()">Cancel</button>
        </div>
    `;
    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => { if (e.target === modal) modal.remove(); });
};

window.recordPayment = function(guestId, amount) {
    window._walkinPayments[guestId] = amount;
    localStorage.setItem('walkin_payments', JSON.stringify(window._walkinPayments));
    const modal = document.getElementById('pay-modal');
    if (modal) modal.remove();
    // Re-render pending to show paid status
    if (typeof renderPending === 'function') renderPending();
    // Also check them in automatically
    window.checkInGroup(guestId, 1);
};

// --- CSV Export ---
window.exportCSV = function() {
    const guests = window.GUESTS || {};
    const scannedData = window.scanned || {};
    const walkinPay = window._walkinPayments || {};
    const rows = [['Name', 'Email', 'Phone', 'Status', 'Amount', 'Screenings', 'Tickets', 'Checked In', 'Check-in Times', 'Walk-in Payment']];
    for (const [id, guest] of Object.entries(guests)) {
        const record = scannedData[id];
        const checkedIn = record ? record.count : 0;
        const times = record ? record.times.join('; ') : '';
        rows.push([
            guest.name,
            guest.email,
            guest.phone,
            guest.status.replace('_', ' '),
            guest.amount,
            guest.screenings,
            guest.quantity,
            checkedIn,
            times,
            walkinPay[id] || ''
        ]);
    }
    const csv = rows.map(r => r.map(v => '"' + String(v).replace(/"/g, '""') + '"').join(',')).join('\\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'checkin_export.csv';
    a.click();
    URL.revokeObjectURL(url);
};
</script>"""
    # --- Google Sheets sync layer (only if --sync-url provided) ---
    if sync_url:
        sync_js = f"""
<script>
// --- Multi-device sync via Google Sheets ---
const SYNC_URL = '{sync_url}';
const MATCH_SLUG = '{slug}';
const DEVICE_ID = localStorage.getItem('arsenal_device_id') || (() => {{
    const id = crypto.randomUUID();
    localStorage.setItem('arsenal_device_id', id);
    return id;
}})();
let syncQueue = JSON.parse(localStorage.getItem(scannedKey + '_queue') || '[]');
let syncStatus = 'unknown';

function updateSyncDot(status) {{
    syncStatus = status;
    const dot = document.getElementById('sync-dot');
    if (dot) dot.className = 'sync-dot ' + status;
}}

async function flushSyncQueue() {{
    if (syncQueue.length === 0) {{ updateSyncDot('synced'); return; }}
    updateSyncDot('pending');
    const batch = [...syncQueue];
    try {{
        await fetch(SYNC_URL, {{
            method: 'POST',
            mode: 'no-cors',
            headers: {{ 'Content-Type': 'text/plain' }},
            body: JSON.stringify({{ events: batch }})
        }});
        // no-cors means we can't read the response, assume success if no error
        syncQueue = syncQueue.slice(batch.length);
        localStorage.setItem(scannedKey + '_queue', JSON.stringify(syncQueue));
        updateSyncDot(syncQueue.length === 0 ? 'synced' : 'pending');
    }} catch (e) {{
        updateSyncDot('offline');
    }}
}}

async function pullRemoteState() {{
    try {{
        const resp = await fetch(SYNC_URL + '?slug=' + encodeURIComponent(MATCH_SLUG));
        if (!resp.ok) return;
        const remote = await resp.json();
        let changed = false;
        for (const [id, data] of Object.entries(remote)) {{
            if (!window.scanned[id] || data.count > window.scanned[id].count) {{
                window.scanned[id] = {{ count: data.count, times: data.times || [] }};
                changed = true;
            }}
        }}
        if (changed) {{
            localStorage.setItem(scannedKey, JSON.stringify(window.scanned));
            updateStats();
            renderPending();
            if (typeof updateFilterCounts === 'function') updateFilterCounts();
        }}
        updateSyncDot('synced');
    }} catch (e) {{
        updateSyncDot('offline');
    }}
}}

function queueSyncEvent(guestId, count, action) {{
    const event = {{
        slug: MATCH_SLUG,
        guest_id: guestId,
        count: count,
        device_id: DEVICE_ID,
        action: action,
        time: new Date().toISOString()
    }};
    syncQueue.push(event);
    localStorage.setItem(scannedKey + '_queue', JSON.stringify(syncQueue));
    flushSyncQueue();
}}

async function pullRemoteGuests() {{
    try {{
        const resp = await fetch(SYNC_URL + '?type=guests&slug=' + encodeURIComponent(MATCH_SLUG));
        if (!resp.ok) return;
        const remote = await resp.json();
        if (Object.keys(remote).length === 0) return;
        // Cache in localStorage
        localStorage.setItem(scannedKey + '_guests', JSON.stringify(remote));
        // Replace window.GUESTS and re-render
        window.GUESTS = remote;
        updateStats();
        renderPending();
        if (typeof updateFilterCounts === 'function') updateFilterCounts();
    }} catch (e) {{}}
}}

let syncStarted = false;
function startSync() {{
    if (syncStarted) return;
    syncStarted = true;
    // Load cached guests first for speed, then fetch fresh
    const cachedGuests = localStorage.getItem(scannedKey + '_guests');
    if (cachedGuests) {{
        try {{
            const parsed = JSON.parse(cachedGuests);
            if (Object.keys(parsed).length > 0) {{
                window.GUESTS = parsed;
                updateStats();
                renderPending();
                if (typeof updateFilterCounts === 'function') updateFilterCounts();
            }}
        }} catch(e) {{}}
    }}
    pullRemoteGuests();
    pullRemoteState().then(() => flushSyncQueue());
    setInterval(() => {{
        pullRemoteState();
        flushSyncQueue();
    }}, 12000);
    // Refresh guests less frequently (every 60s)
    setInterval(pullRemoteGuests, 60000);
}}
</script>"""
        extra_js += sync_js

    html = html.replace("</body>", extra_js + "\n</body>")

    # Patch checkInGroup and resetScans to queue sync events
    if sync_url:
        # After localStorage.setItem in checkInGroup, add sync queue call
        html = html.replace(
            "localStorage.setItem(scannedKey, JSON.stringify(scanned));\n\n        const max = guest.quantity;",
            "localStorage.setItem(scannedKey, JSON.stringify(scanned));\n        if (typeof queueSyncEvent === 'function') queueSyncEvent(id, count, 'checkin');\n\n        const max = guest.quantity;",
        )

        # After localStorage.setItem in resetScans, add sync reset call
        html = html.replace(
            "localStorage.setItem(scannedKey, JSON.stringify(scanned));\n            updateStats();",
            "localStorage.setItem(scannedKey, JSON.stringify(scanned));\n            if (typeof queueSyncEvent === 'function') queueSyncEvent('__ALL__', 0, 'reset');\n            updateStats();",
        )

        # Inject startSync() after PIN gate unlock (fresh entry)
        html = html.replace(
            "document.getElementById('pin-gate').style.display = 'none';\n                document.getElementById('scanner-app').style.display = 'flex';",
            "document.getElementById('pin-gate').style.display = 'none';\n                document.getElementById('scanner-app').style.display = 'flex';\n                if (typeof startSync === 'function') startSync();",
        )

        # Inject startSync() after sessionStorage auto-unlock
        html = html.replace(
            "if (sessionStorage.getItem('scanner_unlocked') === 'true') {\n        document.getElementById('pin-gate').style.display = 'none';\n        document.getElementById('scanner-app').style.display = 'flex';\n    }",
            "if (sessionStorage.getItem('scanner_unlocked') === 'true') {\n        document.getElementById('pin-gate').style.display = 'none';\n        document.getElementById('scanner-app').style.display = 'flex';\n        if (typeof startSync === 'function') setTimeout(startSync, 100);\n    }",
        )

    # Write output
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, f"checkin_{slug}.html")
    with open(output_path, "w") as f:
        f.write(html)

    # Generate live stats page
    stats_path = generate_stats_page(match_info, total_tickets, paid_member_count, paid_guest_count, nonpaid_member_count, slug, sync_url)

    return output_path, stats_path, len(attendees), total_tickets, paid_member_count, paid_guest_count, nonpaid_member_count


def generate_stats_page(match_info, total_tickets, paid_members, paid_guests, nonpaid, slug, sync_url=""):
    """Generate a read-only live stats page that reads from the same localStorage."""
    logo_url = "https://drive.google.com/uc?export=view&id=1FLUcqsjxM3uPmgs5IGhNmkbTF81z0E6J"
    screening_time = match_info.get("screening_time", "")
    venue = match_info.get("venue_name", "")

    stats_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Live Stats - {match_info['display_title']}</title>
    <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --red: #EF0107;
            --bg: #0a0a0a;
            --surface: #141414;
            --surface-2: #1c1c1c;
            --surface-3: #252525;
            --text: #ffffff;
            --text-dim: #777;
            --green: #00C853;
            --amber: #FF9100;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'DM Sans', sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 40px 24px;
            text-align: center;
            -webkit-font-smoothing: antialiased;
        }}
        .logo {{ width: 80px; height: 80px; border-radius: 50%; margin-bottom: 20px; }}
        .match-title {{
            font-family: 'Bebas Neue', sans-serif;
            font-size: 42px;
            letter-spacing: 3px;
            line-height: 1;
        }}
        .match-comp {{
            font-size: 14px;
            color: var(--text-dim);
            letter-spacing: 3px;
            text-transform: uppercase;
            margin-top: 4px;
        }}
        .match-meta {{
            font-size: 13px;
            color: var(--text-dim);
            margin-top: 8px;
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap: 16px;
            margin-top: 40px;
            width: 100%;
            max-width: 500px;
        }}
        .stat-card {{
            background: var(--surface);
            border-radius: 16px;
            padding: 24px 12px;
        }}
        .stat-num {{
            font-family: 'Bebas Neue', sans-serif;
            font-size: 56px;
            line-height: 1;
        }}
        .stat-num.green {{ color: var(--green); }}
        .stat-num.amber {{ color: var(--amber); }}
        .stat-lbl {{
            font-size: 11px;
            color: var(--text-dim);
            letter-spacing: 2px;
            text-transform: uppercase;
            margin-top: 4px;
        }}
        .progress-wrap {{
            margin-top: 32px;
            width: 100%;
            max-width: 500px;
        }}
        .progress-bar {{
            height: 8px;
            background: var(--surface-2);
            border-radius: 4px;
            overflow: hidden;
        }}
        .progress-fill {{
            height: 100%;
            background: var(--green);
            border-radius: 4px;
            transition: width 0.5s ease;
        }}
        .progress-label {{
            font-size: 12px;
            color: var(--text-dim);
            margin-top: 6px;
        }}
        .breakdown {{
            display: flex;
            gap: 16px;
            justify-content: center;
            margin-top: 24px;
            font-size: 12px;
            color: var(--text-dim);
        }}
        .updated {{
            font-size: 11px;
            color: var(--text-dim);
            margin-top: 32px;
            opacity: 0.5;
        }}
    </style>
</head>
<body>
    <img class="logo" src="{logo_url}" alt="Arsenal Pune SC" />
    <div class="match-title">{match_info['display_title'].upper()}</div>
    <div class="match-comp">{match_info['display_subtitle']}</div>
    <div class="match-meta">{"📍 " + venue if venue else ""} {"· 🕘 " + screening_time if screening_time else ""}</div>

    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-num" id="total">{total_tickets}</div>
            <div class="stat-lbl">Tickets</div>
        </div>
        <div class="stat-card">
            <div class="stat-num green" id="checked">0</div>
            <div class="stat-lbl">Checked In</div>
        </div>
        <div class="stat-card">
            <div class="stat-num amber" id="remaining">{total_tickets}</div>
            <div class="stat-lbl">Remaining</div>
        </div>
    </div>

    <div class="progress-wrap">
        <div class="progress-bar"><div class="progress-fill" id="progress" style="width:0%"></div></div>
        <div class="progress-label" id="progress-label">0% checked in</div>
    </div>

    <div class="breakdown">
        <span>🟡 {paid_members} paid members</span>
        <span>⚪ {paid_guests} paid guests</span>
        <span>⚫ {nonpaid} unpaid</span>
    </div>

    <div class="updated" id="updated">Waiting for data...</div>

    <script>
        const TOTAL = {total_tickets};
        const scannedKey = 'arsenal_checkin_{slug}';
        const SYNC_URL = '{sync_url}';
        const MATCH_SLUG = '{slug}';

        function updateUI(scanned) {{
            let checkedIn = 0;
            for (const record of Object.values(scanned)) {{
                checkedIn += record.count || 0;
            }}
            const remaining = TOTAL - checkedIn;
            const pct = TOTAL > 0 ? Math.round((checkedIn / TOTAL) * 100) : 0;

            document.getElementById('checked').textContent = checkedIn;
            document.getElementById('remaining').textContent = remaining;
            document.getElementById('progress').style.width = pct + '%';
            document.getElementById('progress-label').textContent = pct + '% checked in';
            document.getElementById('updated').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
        }}

        async function refresh() {{
            if (SYNC_URL) {{
                try {{
                    const resp = await fetch(SYNC_URL + '?slug=' + encodeURIComponent(MATCH_SLUG));
                    if (resp.ok) {{
                        const remote = await resp.json();
                        updateUI(remote);
                        return;
                    }}
                }} catch (e) {{}}
            }}
            // Fallback to localStorage
            const scanned = JSON.parse(localStorage.getItem(scannedKey) || '{{}}');
            updateUI(scanned);
        }}

        refresh();
        setInterval(refresh, {('10000' if sync_url else '3000')});
    </script>
</body>
</html>"""

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stats_path = os.path.join(OUTPUT_DIR, f"stats_{slug}.html")
    with open(stats_path, "w") as f:
        f.write(stats_html)
    return stats_path


def push_guests_to_sheet(attendees, slug, sheet_id, account):
    """Push guest list to Google Sheets 'Guests' tab via gog CLI."""
    if not shutil.which("gog"):
        print("ERROR: gog CLI not found. Install it or skip --push-guests.")
        return

    # Ensure Guests sheet exists with headers
    print("Pushing guests to Google Sheets...")
    try:
        # Check if sheet has data already for this slug and clear it
        result = subprocess.run(
            ["gog", "sheets", "get", sheet_id, "Guests!A1:A1", "-a", account],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            # Guests sheet doesn't exist, create it by writing headers
            subprocess.run(
                ["gog", "sheets", "update", sheet_id, "Guests!A1:I1",
                 "guest_id", "name", "email", "phone", "amount", "quantity", "status", "screenings", "match_slug",
                 "-a", account],
                check=True, capture_output=True, text=True
            )
            print("  Created 'Guests' sheet with headers")

        # Clear existing guest data (keep headers)
        subprocess.run(
            ["gog", "sheets", "clear", sheet_id, "Guests!A2:I", "-a", account],
            capture_output=True, text=True
        )

        # Build 2D array of all guest rows and append in one call
        rows = []
        for a in attendees:
            rows.append([
                a["msg_id"],
                a["name"],
                a["email"],
                a["phone"],
                str(a["amount"]),
                str(a["ticket_count"]),
                a["status"],
                str(a.get("screenings", 0)),
                slug,
            ])
        values_json = json.dumps(rows)
        subprocess.run(
            ["gog", "sheets", "append", sheet_id, "Guests!A1:I1",
             "--values-json", values_json,
             "-a", account],
            check=True, capture_output=True, text=True
        )
        print(f"  Pushed {len(rows)} guests to Guests sheet")
    except subprocess.CalledProcessError as e:
        print(f"ERROR pushing guests: {e.stderr or e}")


def main():
    parser = argparse.ArgumentParser(description="Generate check-in page for a screening")
    parser.add_argument("--slug", default="2026_03_22_carabao_cup_final_arsenal_v_mancity",
                        help="Match slug from screening_payments")
    parser.add_argument("--sync-url", default="",
                        help="Google Apps Script web app URL for multi-device sync")
    parser.add_argument("--push-guests", action="store_true",
                        help="Push guest list to Google Sheets via gog CLI")
    parser.add_argument("--sheet-id", default="1eChpDkONGG-8dzzElu0WS7LGkesUcJaalPS-cSgZtEY",
                        help="Google Sheets spreadsheet ID")
    parser.add_argument("--gog-account", default="arsenalpune@gmail.com",
                        help="gog CLI account email")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    attendees = get_attendees(conn, args.slug)
    match_info = get_match_info(conn, args.slug)
    conn.close()

    path, stats_path, people, tickets, paid_members, paid_guests, nonpaid = generate_html(attendees, match_info, args.slug, sync_url=args.sync_url)
    print(f"Check-in page: {path}")
    print(f"Live stats page: {stats_path}")
    print(f"  {people} people, {tickets} paid tickets")
    print(f"  {paid_members} paid members, {paid_guests} paid guests, {nonpaid} non-paid members")

    if args.push_guests:
        push_guests_to_sheet(attendees, args.slug, args.sheet_id, args.gog_account)


if __name__ == "__main__":
    main()
