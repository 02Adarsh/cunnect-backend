import html as html_parser

import json

import re

import datetime as _dt_mod

import time

from urllib.parse import urljoin, urlparse


import requests

from bs4 import BeautifulSoup


# ⭐ VERSION STAMP: stale-file deploy pakadne ke liye. Server start /
# reload pe console me ye line DIKHNI chahiye - na dikhe to
# scraper_backend.py replace hi nahi hui (purana copy chal raha hai).
BACKEND_VERSION = "5.22-paidscale"
print(f"[Backend] scraper_backend v{BACKEND_VERSION} loaded")


# ⭐ UIMS static menu-token: is portal pe kai pages BARE GET pe 404
# (error.html) dete hain, sirf ?type=<token> ke saath khulte hain
# (attendance summary page ne ye prove kiya).
_UIMS_STATIC_TOKEN = "etgkYfqBdH1fSfc255iYGw=="


# ⭐ Campus-level "day-wise attendance page NAHI hai" cache: ek poora
# hunt (discovery + guesses) fail ho jaye to 12 ghante tak dobara hunt
# SKIP. Warna har login pe 40-60s sirf isi page ko dhoondhte nikal jaata
# tha (85s authenticate ka sabse bada contributor tha). Key = base_url
# (campus) - page ka exist karna student-depend nahi hota, to EK student
# cost pay karega, baaki sab skip. Page milte hi marker apne aap clear.
_DAILY_NO_PAGE_TTL_SECONDS = 12 * 60 * 60
_DAILY_NO_PAGE = {}


class CUIMSScraperBackend:

    def __init__(self, base_url="https://student.culko.in", uid=None):
        self.base_url = base_url.rstrip("/")

        if self.base_url.lower().endswith("/uims"):
            self.auth_url = self.base_url + "/"
        elif "student.culko.in" in self.base_url.lower():
            self.auth_url = self.base_url + "/"
        else:
            self.auth_url = self.base_url + "/uims/"

        self.uid = uid
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

        self.target_post_url = None
        self.mode = None
        self.payload_stage2_template = {}
        self.captcha_image_bytes = None
        self.captcha_input_name = None

    @staticmethod
    def _hidden_fields(soup):
        data = {}
        for item in soup.find_all("input", type="hidden"):
            name = item.get("name")
            if name:
                data[name] = item.get("value", "")
        return data

    @staticmethod
    def _find_error(soup, default):
        for element_id in ("lblError", "lblMsg", "lblmessage", "Label1"):
            element = soup.find(id=element_id)
            if element and element.get_text(strip=True):
                return element.get_text(strip=True)
        return default

    def execute_stage1(self):
        try:
            response = self.session.get(self.auth_url, timeout=10)
            response.raise_for_status()
        except Exception as exc:
            return {"success": False, "error": f"Failed to reach university portal: {exc}"}

        soup = BeautifulSoup(response.text, "html.parser")
        viewstate = soup.find("input", {"name": "__VIEWSTATE"})

        if not viewstate:
            return {"success": False, "error": "Could not extract ASP.NET viewstate token."}

        payload = self._hidden_fields(soup)
        payload.update({
            "__VIEWSTATE": viewstate.get("value", ""),
            "txtUserId": self.uid,
            "btnNext": "NEXT",
        })

        for button in soup.find_all("input", type="submit"):
            name = button.get("name")
            if name:
                payload[name] = button.get("value", "NEXT")

        try:
            response_stage1 = self.session.post(
                self.auth_url,
                data=payload,
                cookies=response.cookies,
                allow_redirects=False,
                timeout=10,
            )
        except Exception as exc:
            return {"success": False, "error": f"UID validation request failed: {exc}"}

        redirect_url = response_stage1.headers.get("location")

        if redirect_url:
            self.mode = "Redirect"
            redirect_url = html_parser.unescape(redirect_url)
            if redirect_url.startswith("/"):
                redirect_url = urljoin(self.base_url, redirect_url)

            try:
                response_pass = self.session.get(
                    redirect_url,
                    cookies=response_stage1.cookies,
                    timeout=10,
                )
                response_pass.raise_for_status()
            except Exception as exc:
                return {"success": False, "error": f"Failed to pull password page: {exc}"}

            soup_pass = BeautifulSoup(response_pass.text, "html.parser")
            self.target_post_url = redirect_url
            password_html = response_pass.text
        else:
            self.mode = "SamePage"
            soup_pass = BeautifulSoup(response_stage1.text, "html.parser")
            self.target_post_url = self.auth_url
            password_html = response_stage1.text

        password_visible = (
            soup_pass.find("input", {"name": "txtLoginPassword"})
            or soup_pass.find("input", {"id": "txtLoginPassword"})
            or "txtLoginPassword" in password_html
        )

        if not password_visible:
            return {
                "success": False,
                "error": (
                    f"Student UID '{self.uid}' validation failed. "
                    "Password screen was not activated."
                ),
            }

        viewstate_pass = soup_pass.find("input", {"name": "__VIEWSTATE"})
        self.payload_stage2_template = self._hidden_fields(soup_pass)
        self.payload_stage2_template.update({
            "__VIEWSTATE": viewstate_pass.get("value", "") if viewstate_pass else "",
            "txtUserId": self.uid,
            "btnLogin": "LOGIN",
        })

        for button in soup_pass.find_all("input", type="submit"):
            name = button.get("name")
            if name:
                self.payload_stage2_template[name] = button.get("value", "LOGIN")

        captcha_img = None
        for image in soup_pass.find_all("img"):
            image_id = image.get("id", "")
            image_src = image.get("src", "")
            if "captcha" in image_id.lower() or "captcha" in image_src.lower():
                captcha_img = image
                break

        captcha_input = None
        for input_tag in soup_pass.find_all("input"):
            name = input_tag.get("name", "")
            input_id = input_tag.get("id", "")
            if "captcha" in name.lower() or "captcha" in input_id.lower():
                captcha_input = input_tag
                break

        has_captcha = False
        self.captcha_image_bytes = None

        if captcha_img is not None and captcha_input is not None:
            has_captcha = True
            self.captcha_input_name = captcha_input.get("name")
            captcha_url = urljoin(
                self.target_post_url,
                captcha_img.get("src", ""),
            )
            try:
                captcha_response = self.session.get(captcha_url, timeout=10)
                captcha_response.raise_for_status()
                self.captcha_image_bytes = captcha_response.content
            except Exception:
                has_captcha = False

        return {
            "success": True,
            "has_captcha": has_captcha,
            "mode": self.mode,
            "target_post_url": self.target_post_url,
            "captcha_input_name": self.captcha_input_name,
        }

    def execute_stage2(
        self,
        password,
        captcha_code=None,
        stage2_payload=None,
        cached_cookies=None,
    ):
        if cached_cookies:
            self.session.cookies = requests.utils.cookiejar_from_dict(cached_cookies)

        payload = dict(stage2_payload or self.payload_stage2_template)
        payload["txtLoginPassword"] = password

        if captcha_code and self.captcha_input_name:
            payload[self.captcha_input_name] = captcha_code

        try:
            response = self.session.post(
                self.target_post_url,
                data=payload,
                cookies=self.session.cookies,
                allow_redirects=False,
                timeout=10,
            )
        except Exception as exc:
            return {"success": False, "error": f"Password authorization failed: {exc}"}

        if response.status_code not in (301, 302, 303, 307, 308):
            soup = BeautifulSoup(response.text, "html.parser")
            return {
                "success": False,
                "error": self._find_error(
                    soup,
                    "Incorrect password or captcha code. Login rejected.",
                ),
            }

        location = html_parser.unescape(response.headers.get("location", ""))

        if "login" in location.lower():
            error_url = urljoin(self.base_url + "/", location)
            try:
                error_response = self.session.get(error_url, timeout=10)
                error_soup = BeautifulSoup(error_response.text, "html.parser")
                return {
                    "success": False,
                    "error": self._find_error(
                        error_soup,
                        "Invalid Student UID or Password. Access denied.",
                    ),
                }
            except Exception:
                return {"success": False, "error": "Login rejected by university portal."}

        if location:
            next_url = urljoin(self.base_url + "/", location)
            try:
                self.session.get(next_url, cookies=response.cookies, timeout=10)
            except requests.RequestException:
                pass

        return {
            "success": True,
            "cookies": requests.utils.dict_from_cookiejar(self.session.cookies),
        }

    def scrape_section_page(self, page_name, candidate_paths, cookies_dict=None):
        """Fetch and parse an authenticated portal section."""
        if cookies_dict:
            self.session.cookies = requests.utils.cookiejar_from_dict(cookies_dict)

        for path in candidate_paths:
            page_url = urljoin(self.auth_url, path)

            try:
                response = self.session.get(page_url, timeout=10)
                response.raise_for_status()
            except requests.RequestException:
                continue

            if "login" in response.url.lower():
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            page_text = soup.get_text(" ", strip=True)
            lower_text = page_text.lower()

            if any(token in lower_text for token in (
                "page not found",
                "object reference not set",
                "server error",
            )):
                continue

            tables = []
            for table in soup.find_all("table"):
                rows = []
                for row in table.find_all("tr"):
                    cells = row.find_all(["th", "td"])
                    values = [cell.get_text(" ", strip=True) for cell in cells]
                    if values:
                        rows.append(values)
                if rows:
                    tables.append(rows)

            cards = []
            for element in soup.select(
                ".card, .notice, .announcement, .message, "
                ".list-group-item, [class*='notice'], [class*='message']"
            ):
                text = element.get_text(" ", strip=True)
                if text and {"text": text} not in cards:
                    cards.append({"text": text})

            title = soup.title.get_text(" ", strip=True) if soup.title else page_name.title()
            return {
                "success": True,
                "section": page_name,
                "url": response.url,
                "title": title,
                "tables": tables,
                "cards": cards,
                "raw_text": page_text,
            }

        return {
            "success": False,
            "section": page_name,
            "error": f"{page_name} page was not found",
            "tables": [],
            "cards": [],
            "raw_text": "",
        }

    def scrape_announcements(self, cookies_dict=None):
        """Backward-compatible notice scraper used by older views.py."""
        result = self.scrape_section_page(
            page_name="notices",
            candidate_paths=[
                "frmNoticeBoard.aspx",
                "frmNotice.aspx",
                "NoticeBoard.aspx",
                "frmStudentNotice.aspx",
                "frmStudentHome.aspx",
            ],
            cookies_dict=cookies_dict,
        )

        announcements = []
        for row in result.get("tables", []):
            if not row:
                continue
            announcements.append({
                "title": row[0] if len(row) > 0 else "",
                "date": row[1] if len(row) > 1 else "",
                "department": row[2] if len(row) > 2 else "University",
            })

        for card in result.get("cards", []):
            announcements.append({
                "title": card.get("text", ""),
                "date": "",
                "department": "University",
            })

        return {
            "success": result.get("success", False),
            "announcements": announcements,
            "error": result.get("error"),
        }

    # ------------------------------------------------------------------
    # ⭐ STUDENT-HOME ANNOUNCEMENTS (Notices tab)
    # ------------------------------------------------------------------
    _NOTICE_DATE_RE = re.compile(
        r"\d{1,2}(?:st|nd|rd|th)?\s+"
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s,]*\d{2,4}"
        r"|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}",
        flags=re.I,
    )
    _NOTICE_KEYWORD_RE = re.compile(
        r"(announce|notice|circular|news|marquee|ticker|event)", re.I
    )

    def scrape_home_announcements(self, cookies_dict=None):
        """⭐ StudentHome.aspx ke announcements (CU UIMS home page).

        Page ka announcement panel static HTML me KHALI aata hai
        ("Loading Announcement...") - asli list AJAX PageMethod se aati
        hai (UIMSHome4.0.js -> DisplayAnnouncements()):
            POST StudentHome.aspx/DisplayAnnouncements
            {Category:'ALL', PageNumber:'1', FilterText:''}
        Response "d" = "count--IsNewAnnouncement--more?--IsNewAnnouncement--HTML".
        AJAX pehle try, fail ho to static HTML parse (fallback layers).
        """
        if cookies_dict:
            self.session.cookies = requests.utils.cookiejar_from_dict(cookies_dict)

        home_url = self.auth_url + "StudentHome.aspx"

        # ⭐ 1) AJAX PageMethod - yahi asli source hai (attendance ke
        # GetReport jaisa hi pattern).
        ajax_items = self._fetch_announcements_ajax(home_url)
        if ajax_items:
            print(f"[Notices] AJAX announcements={len(ajax_items)}")
            for sample in ajax_items[:3]:
                print(
                    f"[Notice] {sample.get('date', '-')} | "
                    f"{sample.get('title', '')[:80]}"
                )
            return {"success": True, "announcements": ajax_items}

        # 2) Fallback: static HTML parse (agar AJAX structure badal gaya).
        try:
            response = self.session.get(home_url, timeout=10)
            response.raise_for_status()
        except Exception as exc:
            return {
                "success": False,
                "error": f"Failed to reach student home: {exc}",
                "announcements": [],
            }

        if (
            "login" in response.url.lower()
            or "txtloginpassword" in response.text.lower()
        ):
            return {
                "success": False,
                "error": "Portal session expired while opening student home.",
                "announcements": [],
            }

        soup = BeautifulSoup(response.text, "html.parser")
        announcements = self._parse_home_announcements(soup)

        # Debug dump - notices na mile to asli page dekhne ke liye.
        if not announcements:
            try:
                with open("notices_home_debug.html", "w", encoding="utf-8") as debug_file:
                    debug_file.write(response.text)
            except OSError:
                pass

        print(f"[Notices] StudentHome(static) announcements={len(announcements)}")
        return {"success": True, "announcements": announcements}

    def _fetch_announcements_ajax(self, home_url):
        """DisplayAnnouncements PageMethod se announcement HTML kheencho."""
        url = home_url + "/DisplayAnnouncements"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Referer": home_url,
            "X-Requested-With": "XMLHttpRequest",
        }

        items = []
        seen = set()
        raw_debug = ""

        for page in (1, 2, 3):  # safety cap: 3 pages
            payload = "{Category:'ALL',PageNumber:'%d',FilterText:''}" % page
            try:
                response = self.session.post(
                    url,
                    headers=headers,
                    data=payload,
                    timeout=15,
                )
                response.raise_for_status()
                body = response.json()
            except Exception as exc:
                print(f"[Notices] AJAX page {page} failed: {exc}")
                break

            payload_d = body.get("d")
            if not payload_d or payload_d == "0":
                break
            raw_debug = payload_d

            parts = payload_d.split("--IsNewAnnouncement--")
            html_blob = parts[2] if len(parts) >= 3 else ""
            blob_soup = BeautifulSoup(html_blob, "html.parser")
            # ⭐ style/script ka raw text notice title ban jaata tha
            # (".stickto_top { -webkit-transform...") - pehle hata do.
            for junk in blob_soup(["style", "script", "noscript", "link"]):
                junk.decompose()
            for title, date, desc, files in self._announcement_items_from_html(blob_soup):
                key = (title + "|" + date).lower()
                if key in seen:
                    continue
                seen.add(key)
                items.append({
                    "title": title[:240],
                    "date": date,
                    # ⭐ POORA notice rakho (official letter + schedule);
                    # template collapsible dikhata hai.
                    "desc": desc[:4000],
                    # ⭐ Attachment download links (FileExplorerDownload.ashx)
                    "files": files[:4],
                    "department": "University",
                })

            has_more = len(parts) >= 2 and parts[1] == "1"
            if not has_more:
                break

        if raw_debug:
            # Structure fine-tune karne ke liye asli blob hamesha save rakho
            # (notices_ajax_debug.txt - pichle page ka raw "d").
            try:
                with open("notices_ajax_debug.txt", "w", encoding="utf-8") as debug_file:
                    debug_file.write(raw_debug)
            except OSError:
                pass
        return items

    # ⭐ Notice card me time bhi hota hai: "TITLE 18:27:05 description..."
    # time ke PEHLE wala part = title, BAAD wala = description.
    _NOTICE_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
    # UIMS cards me title plain text hota hai, <a> sirf PDF attachment hota
    # hai ("Attachment_1_Academic...pdf", "80_&_82_Evening...xlsx").
    _FILE_EXT_RE = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|jpe?g|png|zip|rar)\b", re.I)
    _FILE_NAME_RE = re.compile(
        r"^(attachment|annexure|att|file)\b|^\d+[_\s]|[&_]",
        re.I,
    )

    def _announcement_items_from_html(self, soup):
        """Announcement HTML blob se (title, date) pairs nikaalo.

        ⭐ Structure (decoded): blob me `<div id="div_Announcements">` ke
        andar har announcement ek CARD/div hai jisme title (plain text),
        ek date, aur attachment ke download <a> links hote hain. Anchor
        ke text FILE NAMES hote hain, REAL title NAHI - isliye pehle
        attachment links hata do, phir card ka text lo.
        """
        results = []

        container = soup.find(id=re.compile(r"div_Announcements", re.I)) or soup

        # Attachment/download jaisa link? (title kabhi file name nahi hota)
        def is_attachment(anchor):
            text = anchor.get_text(" ", strip=True)
            href = anchor.get("href", "") or ""
            if "download" in href.lower() or self._FILE_EXT_RE.search(href):
                return True
            if not text:
                return True
            if self._FILE_EXT_RE.search(text):
                return True
            if self._FILE_NAME_RE.match(text):
                return True
            return False

        def extract(element):
            # ⭐ Attachment links PEHLE collect karo (download buttons ke
            # liye), phir DOM se hatao - text me filename na lage.
            files = []
            for anchor in element.find_all("a"):
                if not is_attachment(anchor):
                    continue
                name = anchor.get_text(" ", strip=True)[:60]
                href = anchor.get("href", "") or ""
                url = ""
                if href and "javascript:" not in href.lower() and not href.startswith("#"):
                    # FileExplorerDownload.ashx jaisi links absolute banao
                    url = urljoin(self.auth_url + "StudentHome.aspx", href)
                if name and url.startswith("http"):
                    files.append({"name": name, "url": url})
                anchor.decompose()
            text = element.get_text(" ", strip=True)
            if not text or "loading" in text.lower():
                return None
            date_match = self._NOTICE_DATE_RE.search(text)
            date = date_match.group(0) if date_match else ""
            if date_match:
                body = text[:date_match.start()] + " " + text[date_match.end():]
            else:
                body = text
            # ⭐ "TITLE 18:27:05 description..." - time ke pehle TITLE,
            # baad me POORA notice body (letter + schedule + sab kuch).
            time_match = self._NOTICE_TIME_RE.search(body)
            if time_match:
                title = body[:time_match.start()]
                desc = body[time_match.end():]
            else:
                title = body
                desc = body  # time na mile to poora content description me
            title = re.sub(r"\s+", " ", title).strip(" -|•·\t:,")
            desc = re.sub(r"\s+", " ", desc).strip(" -|•·\t:,")
            if len(title) < 10:
                # Title bahut chota ho to desc se title banao, desc poora rakho
                if len(desc) >= 10:
                    title = desc[:60].rsplit(" ", 1)[0] or desc[:60]
                    return title, date, desc, files
                return None
            return title, date, desc, files

        # 1) ⭐ Card-level scan: container ke DIRECT children = ek-ek notice.
        # Kis LEVEL pe cards hain ye "date density" se decide karo: jis
        # level me sabse zyada children ke andar date mile, wahi cards hain
        # (wrapper <form>/<div> ke andar mat ghus jao, single-card case me
        # bhi card ke fragments nahi bante).
        children = container.find_all(recursive=False)
        best_children = children
        best_score = -1
        while children:
            score = sum(
                1 for child in children
                if self._NOTICE_DATE_RE.search(
                    child.get_text(" ", strip=True)
                )
            )
            if score > best_score:
                best_score = score
                best_children = children
            if len(children) == 1:
                children = children[0].find_all(recursive=False)
                continue
            break
        cards = best_children
        if not cards:
            cards = container.find_all(
                ["div", "li", "article", "tr"],
            )
        for card in cards:
            # Card ke andar sub-cards ho sakte hain - sirf leaf-level try
            pair = extract(card)
            if pair:
                results.append(pair)
        if results:
            return results

        # 2) Last fallback: non-attachment anchors.
        for anchor in container.find_all("a"):
            if is_attachment(anchor):
                continue
            pair = extract(anchor)
            if pair:
                results.append(pair)
        return results

    def _parse_home_announcements(self, soup):
        """StudentHome soup se (title, date) announcement rows nikaalo."""
        announcements = []
        seen = set()

        def add(title, date=""):
            title = re.sub(r"\s+", " ", str(title)).strip(" -|•· \t")
            if len(title) < 10:
                return
            lowered = title.lower()
            if lowered in seen:
                return
            if lowered in ("home", "logout", "profile", "dashboard"):
                return
            seen.add(lowered)
            announcements.append({
                "title": title[:180],
                "date": date,
                "department": "University",
            })

        # 1) GridView tables: id/class me notice-keyword, ya header row
        #    me "date" + (title/description/announcement).
        for table in soup.find_all("table"):
            table_identity = " ".join(
                [table.get("id", ""), " ".join(table.get("class", []))]
            )
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            header_text = " ".join(
                cell.get_text(" ", strip=True)
                for cell in rows[0].find_all(["th", "td"])
            ).lower()
            looks_notice = bool(self._NOTICE_KEYWORD_RE.search(table_identity)) or (
                "date" in header_text
                and any(word in header_text for word in (
                    "title", "description", "announce",
                    "notice", "news", "detail",
                ))
            )
            if not looks_notice:
                continue
            for row in rows[1:]:
                cells = [
                    cell.get_text(" ", strip=True)
                    for cell in row.find_all(["td", "th"])
                ]
                if not cells:
                    continue
                blob = " ".join(cells)
                date_match = self._NOTICE_DATE_RE.search(blob)
                date = date_match.group(0) if date_match else ""
                # Title = sabse lamba cell jo sirf date nahi hai.
                title = max(
                    (cell for cell in cells if cell != date),
                    key=len,
                    default="",
                )
                add(title, date)

        # 2) <marquee> blocks (UIMS scrolling notice ticker).
        for marquee in soup.find_all("marquee"):
            anchors = marquee.find_all("a")
            if anchors:
                for anchor in anchors:
                    add(anchor.get_text(" ", strip=True))
            else:
                text = marquee.get_text(" ", strip=True)
                # "12 Jul 2026: first ... 15 Jul 2026: second" pattern
                # ke liye date ke saath split karo.
                chunks = self._NOTICE_DATE_RE.split(text)
                if len(chunks) > 2:
                    dates = self._NOTICE_DATE_RE.findall(text)
                    for index, piece in enumerate(chunks):
                        if piece and len(piece.strip()) >= 10:
                            add(piece, dates[index - 1] if index > 0 else "")
                else:
                    add(text)

        # 3) Notice-keyword wale containers ke anchors / list items.
        containers = (
            soup.find_all(["div", "span", "ul", "section"], id=self._NOTICE_KEYWORD_RE)
            + soup.find_all(["div", "span", "ul", "section"], class_=self._NOTICE_KEYWORD_RE)
        )
        for element in containers:
            for anchor in element.find_all("a"):
                add(anchor.get_text(" ", strip=True))
            for item in element.find_all("li"):
                add(item.get_text(" ", strip=True))

        # ⭐ 4) LAST-RESORT fallback: kisi bhi table row me ek cell
        # bilkul-date ho aur dusra lamba text - use announcement maan lo.
        # UIMS home page master-layout ke plain tables me bhi notices isse
        # aa jaate hain. Sirf tab chalta hai jab upar se kuch na mila ho.
        if not announcements:
            for row in soup.find_all("tr"):
                cells = [
                    cell.get_text(" ", strip=True)
                    for cell in row.find_all(["td", "th"])
                ]
                if len(cells) < 2:
                    continue
                date_cells = [
                    cell for cell in cells
                    if self._NOTICE_DATE_RE.fullmatch(cell.strip())
                ]
                text_cells = [
                    cell for cell in cells
                    if len(cell) >= 15 and cell not in date_cells
                ]
                if not date_cells or not text_cells:
                    continue
                add(max(text_cells, key=len), date_cells[0])

        return announcements[:30]

    # ⭐ Hostel page ke liye blind guesses - asli source StudentHome ke
    # menu links hai (neeche discovery), ye sirf fallback hai.
    _HOSTEL_GUESS_PATHS = (
        "frmHostelStudentDetail.aspx",
        "frmHostelStudentDetails.aspx",
        "frmStudentHostelDetail.aspx",
    )

    def scrape_hostel_details(self, cookies_dict=None):
        """⭐ Hostel details (room/allotment/warden/mess) - best effort.

        Portal UIMS me hostel page ka exact naam campus/instance ke hisaab
        se badalta hai, isliye strategy:
          1) StudentHome.aspx ke saare links scan karo - jinke text/href me
             'hostel' ho, wahi ASLI page hai (discovery-first, no guessing).
          2) Kuch na mile to 2-3 common UIMS Hostel form names try karo.
        Har page se label:value rows (kv) aur tables (sections) generic
        parse karta hai. Debug ke liye HAMESHA hostel_debug.txt dump hota
        hai - agar output galat/khali aaye to wahi file paste karo.

        SAVDHANI: guessed endpoints kabhi-kabhi Login.aspx pe redirect
        karke portal session kill kar dete hain. views me is scrape ko
        health-probe se PEHLE bulaya gaya hai taaki probe session wapas
        restore kar sake.
        """
        if cookies_dict:
            self.session.cookies = requests.utils.cookiejar_from_dict(cookies_dict)

        result = {
            "success": False,
            "found": False,
            "source_url": "",
            "page_title": "",
            "kv": [],
            "sections": [],
        }
        debug_lines = []

        home_url = self.auth_url + "StudentHome.aspx"
        candidates = []

        # ── 1) StudentHome ke menu/links me 'hostel' khojo (asli page) ──
        try:
            home = self.session.get(home_url, timeout=10)
            debug_lines.append(
                f"HOME status={home.status_code} url={home.url} len={len(home.text)}"
            )
            if "login" not in home.url.lower():
                home_soup = BeautifulSoup(home.text, "html.parser")
                for anchor in home_soup.find_all("a", href=True):
                    href = anchor["href"].strip()
                    blob = (
                        anchor.get_text(" ", strip=True) + " " + href
                    ).lower()
                    if "hostel" in blob and ".aspx" in href.lower():
                        if not href.lower().startswith("javascript"):
                            candidates.append(urljoin(self.auth_url, href))
                # Raw HTML me bhi frm*Hostel*.aspx jaisa mention ho sakta
                # hai (menu JS se banta hai, anchor nahi hota).
                for match in re.findall(
                    r"[\w/&?=.%-]*[Hh]ostel[\w/&?=.%-]*\.aspx(?:\?[\w=&%.-]*)?",
                    home.text,
                ):
                    if "login" not in match.lower():
                        candidates.append(urljoin(self.auth_url, match))
        except Exception as exc:
            debug_lines.append(f"HOME error: {exc}")

        # ── 2) Guesses fallback (agar discovery khaali rahe) ──
        candidates.extend(self.auth_url + p for p in self._HOSTEL_GUESS_PATHS)

        # Dedupe, order preserve, max 4 attempts (login fast rakho).
        seen = set()
        ordered = []
        for url in candidates:
            key = url.split("?")[0].lower()
            if key not in seen:
                seen.add(key)
                ordered.append(url)
        ordered = ordered[:4]

        kv, sections, page_title, source_url = [], [], "", ""
        for url in ordered:
            try:
                resp = self.session.get(url, timeout=10, allow_redirects=True)
            except Exception as exc:
                debug_lines.append(f"TRY {url} -> error {exc}")
                continue
            final = resp.url
            title = ""
            try:
                soup = BeautifulSoup(resp.text, "html.parser")
                if soup.title and soup.title.string:
                    title = soup.title.string.strip()
            except Exception:
                soup = None
            debug_lines.append(
                f"TRY {url} -> status={resp.status_code} final={final} "
                f"title={title[:70]!r} len={len(resp.text)}"
            )
            if soup is None:
                continue
            # Session kill / login redirect ho to AAGE guesses mat maaro -
            # sirf nuksan hoga. Ruk jao (views ka probe session bachayega).
            if (
                "login" in final.lower()
                or "txtloginpassword" in resp.text.lower()
            ):
                debug_lines.append("LOGIN-REDIRECT detected; stopping attempts.")
                break
            found_kv, found_sections = self._parse_hostel_page(soup)
            debug_lines.append(
                f"  parsed kv={len(found_kv)} sections={len(found_sections)}"
            )
            if found_kv or found_sections:
                kv, sections = found_kv, found_sections
                page_title, source_url = title, final
                break

        result["found"] = bool(kv or sections)
        result["success"] = result["found"]
        result["kv"] = kv[:14]
        result["sections"] = sections[:3]
        result["page_title"] = page_title
        result["source_url"] = source_url

        hint = (
            "StudentHome menu me 'hostel' link nahi mila - ya to hostel "
            "module off hai, ya page ka naam alag hai."
            if not result["found"] else ""
        )
        debug_lines.append(
            f"RESULT found={result['found']} kv={len(kv)} sections={len(sections)}"
        )
        if hint:
            debug_lines.append("HINT " + hint)

        try:
            with open("hostel_debug.txt", "w", encoding="utf-8") as debug_file:
                debug_file.write("\n".join(debug_lines))
        except OSError:
            pass

        print(
            f"[Hostel] found={result['found']} kv={len(result['kv'])} "
            f"sections={len(result['sections'])} src={source_url or '-'}"
        )
        return result

    # ------------------------------------------------------------------
    # ⭐ DAY-WISE ATTENDANCE - "kis din P, kis din A"
    # ------------------------------------------------------------------
    _DAILY_DATE_RE = re.compile(
        r"\d{1,2}\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
        r"(?:[\s,\-/]*\d{2,4})?|\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}",
        re.I,
    )
    _DAILY_STATUS_RE = re.compile(
        r"^(p|a|present|absent|od|dl|ml|duty|on\s*duty|medical|leave|"
        r"holiday|off|suspended|na)$",
        re.I,
    )
    _DAILY_TIME_RE = re.compile(
        r"\d{1,2}:\d{2}\s*(?:am|pm)?(?:\s*[-–—]\s*\d{1,2}:\d{2}\s*(?:am|pm)?)?",
        re.I,
    )
    # ⭐ Blind guesses sirf FALLBACK - pehle asli page portal ke links se
    # discovery hoti hai (hostel/profile wala hi proven pattern).
    _DAILY_GUESS_PATHS = (
        "frmStudentAttendanceDetail.aspx",
        "frmStudentAttendanceDetails.aspx",
        "frmStudentDayWiseAttendance.aspx",
        "frmMyAttendance.aspx",
        "frmAttendanceDetails.aspx",
        "frmStudentDailyAttendance.aspx",
    )
    # ⭐ Sibling guesses: summary page to EXIST karta hi hai
    # (frmStudentCourseWiseAttendanceSummary) - usi family ke
    # Detail/Report cousins sabse probable asli pages hain.
    _DAILY_SIBLING_GUESSES = (
        "frmStudentCourseWiseAttendanceDetail.aspx",
        "frmStudentCourseWiseAttendance.aspx",
        "frmStudentDayWiseAttendanceReport.aspx",
    )

    @staticmethod
    def _daily_tone(status):
        s = str(status).strip().lower()
        if s in ("p", "present", "od", "dl", "duty", "on duty"):
            return "present"
        if s in ("a", "absent"):
            return "absent"
        return "leave"

    def scrape_daily_attendance(self, cookies_dict=None, encrypt_codes=None):
        """⭐ Day-wise attendance (date -> kaunsi lecture P/A thi).

        Course-wise SUMMARY (GetReport) ATTENDANCE tab me aa hi rahi hai -
        ye uska DETAIL log hai (PRESENT/ABSENT day-by-day). Exact page ka
        naam campus ke hisaab se badalta hai, isliye (debug se seekha):
          1) DISCOVERY: TOKEN'd summary + StudentHome ke anchors +
             ENCRYPT-CONTEXT mining (EncryptCode value ke aas-paas ka
             drill-link) + same-origin JS-FILE mining (menu ke asli
             page-names external .js me hote hain - landing HTML me
             BILKUL nahi milte, live debug ne prove kiya)
          2) TOKEN: encrypt_codes (attendance records ka EncryptCode)
             se ?type= / ?code= variants - ye portal BARE detail GET
             pe 404 (error.html) deta hai, token zaroori
          3) FALLBACK: sibling guesses (asli summary-family cousins) +
             common UIMS guess paths (sirf GET, kabhi POST nahi)
        Parser do layouts samajhta hai (_parse_daily_attendance_page):
          ROW-form    - har row: Date | Time | Course | Status(P/A)
          MATRIX-form - header row me dates, niche course x P/A cells

        Session safety: login redirect mila to TURANT stop (views me ye
        scrape health-probe ke PEHLE bulaya gaya hai, probe restore karega).

        Debug: HAMESHA attendance_daily_debug.txt - khaali/galat aaye to
        wahi file paste karo, exact page structure wahin se milega.
        """
        if cookies_dict:
            self.session.cookies = requests.utils.cookiejar_from_dict(cookies_dict)

        result = {
            "success": False,
            "found": False,
            "days": [],
            "subjects": [],
            "stats": {"present": 0, "absent": 0, "pct": 0},
            "records": 0,
            "source_url": "",
            "page_title": "",
        }

        # ⭐ Campus no-page cache: pichla poora hunt fail hua to 12h tak
        # turant skip (har login pe 40-60s bachte hain - page student pe
        # depend nahi karta, campus-level cheez hai). NOTE: skip pe debug
        # file OVERWRITE nahi karte - pichli FULL-HUNT wali file (links/
        # TRY lines) disk pe bachi rehti hai.
        # ⭐ Key me BACKEND_VERSION: naye hunt-logic ke deploy pe purana
        # "nahi mila" marker apne aap bypass ho jaata hai (12h wait nahi).
        cache_key = f"{self.base_url.lower()}|{BACKEND_VERSION}"
        no_page_at = _DAILY_NO_PAGE.get(cache_key, 0)
        if no_page_at and (time.time() - no_page_at) < _DAILY_NO_PAGE_TTL_SECONDS:
            print(
                "[DailyAtt] skip - campus-cache: is campus ka day-wise "
                "page nahi hai (12h tak hunt band)"
            )
            return result

        debug = []
        portal_ok = False  # kahin se bhi non-login page aaya tha (portal zinda)
        # ⭐ EncryptCodes (attendance records ka per-course token) - DETAIL
        # pages ki chaabi. Pehla code token-variants ke liye, top-3 CTX
        # context-mining ke liye (summary grid ka drill-link pattern).
        enc_codes = []
        for code in encrypt_codes or []:
            code = str(code or "").strip()
            if code and code not in enc_codes:
                enc_codes.append(code)
        enc = enc_codes[0] if enc_codes else ""
        debug.append(f"TOKEN encrypt-codes={len(enc_codes)}")
        # ⭐ Soft deadline: hunt kabhi 40s se zyada nahi chalega (portal
        # slow ho to bhi login block nahi hota; campus-cache baaki sab
        # protect karta hi hai).
        deadline = time.time() + 40.0
        landed = []  # (final_url, raw_text, soup) - JS mining ka input
        link_urls, mined_names, mined_urls = [], [], []
        ctx_urls = []    # EncryptCode ke aas-paas mile drill URLs
        menu_urls = []   # menu-partial .aspx (fetch karke dobara mine)
        svc_hits = []    # .asmx / PageMethods / GetMenu* service hints
        aspx_index = set()  # SAARE .aspx names (sitemap - debug ke liye)
        summary_raw = ""  # summary landing ka raw HTML (handshake keys)
        svc_methods = []  # page ke inline .aspx/Method endpoints (GetReport...)
        fn_methods = []   # inline getXxx('...') calls se derived method names

        # ── 1) DISCOVERY: TOKEN'd summary + StudentHome ke anchors + raw
        #    HTML se day-wise page khojo. DO seekh (live debug se):
        #    a) summary page BARE GET pe 404/error.html deta hai - TOKEN'd
        #       URL zaroori (scrape_attendance_records wala static token)
        #    b) sidebar menu JS se banta hai - anchors me asli href nahi
        #       hota; isliye raw HTML se .aspx page-NAMES mine karte hain
        _skip_words = ("summary", "timetable", "reportviewer")
        for landing_url in (
            self.auth_url + "frmStudentCourseWiseAttendanceSummary.aspx"
            "?type=" + _UIMS_STATIC_TOKEN,
            self.auth_url + "StudentHome.aspx",
        ):
            short = landing_url.split("/")[-1][:34]
            try:
                resp = self.session.get(
                    landing_url, timeout=6, allow_redirects=True
                )
            except Exception as exc:
                debug.append(f"LAND {short} -> error {exc}")
                continue
            debug.append(
                f"LAND {short} -> {resp.status_code} final={resp.url}"
            )
            if "login" in resp.url.lower() or "error" in resp.url.lower():
                continue
            if resp.status_code == 200:
                portal_ok = True
            try:
                lsoup = BeautifulSoup(resp.text, "html.parser")
            except Exception:
                continue
            landed.append((resp.url, resp.text, lsoup))
            if "CourseWiseAttendanceSummary" in landing_url:
                # ⭐ PROBE phase ke liye raw HTML chahiye (CurrentSession /
                # getReport handshake keys isi se nikalti hain)
                summary_raw = resp.text
            # 1a) anchor links (href + text keyword)
            for anchor in lsoup.find_all("a", href=True):
                href = anchor["href"].strip()
                low = href.lower()
                if ".aspx" not in low or low.startswith("javascript"):
                    continue
                if any(w in low for w in _skip_words):
                    continue
                blob = (
                    anchor.get_text(" ", strip=True) + " " + low
                ).replace("attendancesummary", "")
                if "attend" in blob or "day-wise" in blob.replace(" ", ""):
                    link_urls.append(urljoin(resp.url, href))
                    debug.append(
                        f"LINK [{short}] "
                        f"{anchor.get_text(' ', strip=True)[:36]!r} -> {href[:90]}"
                    )
            # 1b) ⭐ RAW-HTML MINING: .aspx page-NAMES kahin bhi (scripts /
            # menu JS ke andar bhi - "ShowPage('frmXxx.aspx')" jaisi).
            for name in re.findall(
                r"[A-Za-z_]*(?:[Aa]ttend|[Dd]ay[Ww]ise)[A-Za-z_]*\.aspx",
                resp.text,
            ):
                if any(w in name.lower() for w in _skip_words):
                    continue
                abs_url = urljoin(resp.url, name)
                if abs_url.lower() not in {u.lower() for u in mined_names}:
                    mined_names.append(abs_url)
                    debug.append(f"MINE [{short}] {name}")
            # 1c) ⭐ Query-string wale detail URLs (JS ke ?type= wale)
            for match in re.findall(
                r"[A-Za-z_]*(?:[Aa]ttend|[Dd]ay[Ww]ise)[A-Za-z_]*\.aspx"
                r"\?[A-Za-z0-9=&%_.\-]{2,80}",
                resp.text,
            ):
                if "summary" in match.lower():
                    continue
                abs_url = urljoin(resp.url, match)
                if abs_url.lower() not in {u.lower() for u in mined_urls}:
                    mined_urls.append(abs_url)
                    debug.append(f"MINED-URL [{short}] {match[:100]}")
            # 1d) ⭐ SITEMAP-INDEX: SAARE .aspx names uthao - attend-filter
            #    se bacha hua koi page bhi debug me dikhe, taaki exact naam
            #    pakad sakein.
            for nm in re.findall(r"[A-Za-z_][A-Za-z0-9_/]*\.aspx", resp.text):
                aspx_index.add(nm)
            # 1e) ⭐ ENCRYPT-CONTEXT MINING: summary grid ke rows apna
            #    EncryptCode drill-link/JS-call ke saath rakhte hain. Code
            #    ki RAW VALUE dhoondo, context dump karo (CTX line) aur
            #    usi window se .aspx URL nikaal lo - naam-filter ki
            #    zaroorat hi nahi, value khud raasta bata deti hai!
            for code in enc_codes[:3]:
                start = 0
                hits = 0
                while hits < 3:
                    idx = resp.text.find(code, start)
                    if idx < 0:
                        break
                    start = idx + len(code)
                    hits += 1
                    ctx = re.sub(
                        r"\s+", " ",
                        resp.text[max(0, idx - 200): idx + len(code) + 100],
                    )
                    debug.append(f"CTX [{short}] ...{ctx[:260]}...")
                    ctx_matches = re.findall(
                        r"(?:href|HREF)\s*=\s*['\"]([^'\"]+\.aspx[^'\"]*)['\"]",
                        ctx,
                    ) + re.findall(
                        r"(?:ShowPage|showpage|window\.open|location\.href)"
                        r"\s*[=(]\s*['\"]([^'\"]+\.aspx[^'\"]*)['\"]",
                        ctx,
                    )
                    for m in ctx_matches:
                        if any(w in m.lower() for w in _skip_words):
                            continue
                        abs_u = urljoin(resp.url, m)
                        if abs_u.lower() not in {u.lower() for u in ctx_urls}:
                            ctx_urls.append(abs_u)
                            debug.append(f"CTX-URL [{short}] {m[:100]}")
            # 1f) ⭐ AJAX-ENDPOINT mining (inline JS): attendance records
            #    khud isi page ke /GetReport webmethod se aate hain -
            #    day-wise detail bhi kisi sibling .aspx/Method ya
            #    getXxx('...') call me hogi. Naam + arg dono pakad lo.
            for ep_method in re.findall(
                r"[A-Za-z_][A-Za-z0-9_/]*\.aspx/([A-Za-z0-9_]+)", resp.text
            ):
                if ep_method not in svc_methods:
                    svc_methods.append(ep_method)
                    debug.append(f"SVC [{short}] method={ep_method}")
            for svc in re.findall(
                r"[A-Za-z_][A-Za-z0-9_/]*\.asmx(?:/[A-Za-z0-9_]+)?"
                r"|PageMethods\.[A-Za-z0-9_]+",
                resp.text,
            ):
                if svc not in svc_hits and len(svc_hits) < 12:
                    svc_hits.append(svc)
                    debug.append(f"SVC [{short}] {svc}")
            for fn, arg in re.findall(
                r"\b((?:get|fetch|load|show|view|display|bind)"
                r"[A-Za-z0-9]*(?:[Rr]eport|[Dd]etail|[Ll]ist|[Dd]ay[Ww]ise|"
                r"[Aa]ttend[A-Za-z0-9]*)[A-Za-z0-9]*)\s*\(\s*['\"]?([^'\")]{0,60})",
                resp.text,
            ):
                meth = fn[0].upper() + fn[1:]
                if meth not in fn_methods and len(fn_methods) < 10:
                    fn_methods.append(meth)
                    debug.append(f"FN [{short}] {fn}('{arg[:28]}')" )

        # ── 2) ⭐ JS-FILE MINING: sidebar menu is portal pe JS se banta
        #    hai AUR landing HTML me page-names bilkul nahi milte (v3.6
        #    live debug ne prove kiya - sirf summary ka SELF-reference
        #    tha). Ab same-origin <script src> .js files fetch karke UNKE
        #    andar .aspx names + service endpoints mine karte hain - menu
        #    wahin likha hota hai.
        js_srcs, seen_js = [], set()
        # ⭐ Pure-library js budget waste karti hain (jquery waghaira me
        # kabhi portal page-names nahi hote) - skip; axd bundles rake
        # kyunki WebResource.axd me menu-script ho sakta hai.
        _junk_js = (
            "jquery", "bootstrap", "popper", "moment", "chart", "sweet",
            "apprise", "modernizr", "datatable", "select2", "toastr",
            "validate", "fontawesome", "owl", "slick", "ckeditor",
            "tinymce", "summernote", "pace", "nprogress",
        )
        for final_url, _raw, lsoup2 in landed:
            for tag in lsoup2.find_all("script", src=True):
                src = (tag.get("src") or "").strip()
                if not src or src.lower().startswith(("data:", "javascript:")):
                    continue
                abs_js = urljoin(final_url, src)
                if urlparse(abs_js).netloc != urlparse(self.auth_url).netloc:
                    continue
                low_js = abs_js.lower()
                if any(j in low_js for j in _junk_js):
                    continue
                if low_js in seen_js:
                    continue
                seen_js.add(low_js)
                js_srcs.append(abs_js)

        def _js_rank(u):  # menu/app/common jaise js pehle
            low = u.lower()
            return 0 if any(
                w in low for w in (
                    "menu", "nav", "custom", "app", "main",
                    "site", "student", "home", "common",
                )
            ) else 1

        js_srcs.sort(key=_js_rank)
        for js_url in js_srcs[:6]:
            if time.time() > deadline:
                debug.append("DEADLINE 40s - JS mining yahin roki")
                break
            jshort = js_url.split("?")[0].split("/")[-1][:34]
            try:
                jresp = self.session.get(js_url, timeout=6)
            except Exception as exc:
                debug.append(f"JS {jshort} -> error {exc}")
                continue
            debug.append(
                f"JS {jshort} -> {jresp.status_code} len={len(jresp.text)}"
            )
            if jresp.status_code != 200 or not jresp.text:
                continue
            for nm in re.findall(
                r"[A-Za-z_][A-Za-z0-9_/]*\.aspx(?:\?[A-Za-z0-9=&%_.\-]{2,80})?",
                jresp.text,
            ):
                base_nm, _, qs = nm.partition("?")
                aspx_index.add(base_nm)
                low = base_nm.lower()
                if any(w in low for w in _skip_words):
                    continue
                flat = low.replace("-", "").replace("_", "")
                if "attend" in flat or "daywise" in flat or "datewise" in flat:
                    # ⭐ JS ke andar page-names SITE-ROOT ke relative hote
                    # hain, js-file ke folder ke nahi - warna
                    # /assets/js/frmXxx.aspx jaisa galat path ban jaata.
                    abs_u = urljoin(self.auth_url, nm if qs else base_nm)
                    if qs:
                        if abs_u.lower() not in {x.lower() for x in mined_urls}:
                            mined_urls.append(abs_u)
                            debug.append(f"MINE-JS [{jshort}] {nm[:100]}")
                    elif abs_u.lower() not in {x.lower() for x in mined_names}:
                        mined_names.append(abs_u)
                        debug.append(f"MINE-JS [{jshort}] {base_nm}")
                elif any(
                    w in flat for w in (
                        "menu", "navbar", "navigation", "sidebar", "leftpanel",
                    )
                ):
                    # ⭐ Same root-relative fix (site root se join karo)
                    abs_m = urljoin(self.auth_url, base_nm)
                    if abs_m.lower() not in {x.lower() for x in menu_urls}:
                        menu_urls.append(abs_m)
                        debug.append(f"MENU-HINT [{jshort}] {base_nm}")
            for svc in re.findall(
                r"[A-Za-z_][A-Za-z0-9_/]*\.asmx(?:/[A-Za-z0-9_]+)?"
                r"|PageMethods\.[A-Za-z0-9_]+"
                r"|GetMenu[A-Za-z0-9_]*|LoadMenu[A-Za-z0-9_]*",
                jresp.text,
            ):
                if svc not in svc_hits and len(svc_hits) < 8:
                    svc_hits.append(svc)
                    debug.append(f"SVC [{jshort}] {svc}")

        # ── 3) ⭐ MENU-PARTIAL FETCH: koi menu/navigation .aspx mila ho to
        #    wo HTML partial hota hai jisme saare asli links honge -
        #    fetch karke attend anchors/names dobara mine karo.
        for m_url in menu_urls[:2]:
            if time.time() > deadline:
                break
            mshort = m_url.split("/")[-1][:30]
            try:
                mresp = self.session.get(m_url, timeout=6)
            except Exception as exc:
                debug.append(f"MENU {mshort} -> error {exc}")
                continue
            debug.append(
                f"MENU {mshort} -> {mresp.status_code} len={len(mresp.text)}"
            )
            if mresp.status_code != 200:
                continue
            try:
                msoup = BeautifulSoup(mresp.text, "html.parser")
            except Exception:
                continue
            for anchor in msoup.find_all("a", href=True):
                href = anchor["href"].strip()
                low = href.lower()
                if ".aspx" not in low or low.startswith("javascript"):
                    continue
                if any(w in low for w in _skip_words):
                    continue
                blob = (anchor.get_text(" ", strip=True) + " " + low).replace(
                    "attendancesummary", ""
                )
                if "attend" in blob or "day-wise" in blob.replace(" ", ""):
                    link_urls.append(urljoin(mresp.url, href))
                    debug.append(
                        f"MENU-LINK {anchor.get_text(' ', strip=True)[:36]!r}"
                        f" -> {href[:90]}"
                    )
            for nm in re.findall(r"[A-Za-z_][A-Za-z0-9_/]*\.aspx", mresp.text):
                aspx_index.add(nm)
                low2 = nm.lower()
                if any(w in low2 for w in _skip_words):
                    continue
                flat2 = low2.replace("-", "").replace("_", "")
                if "attend" in flat2 or "daywise" in flat2 or "datewise" in flat2:
                    abs_n = urljoin(mresp.url, nm)
                    if abs_n.lower() not in {x.lower() for x in mined_names}:
                        mined_names.append(abs_n)
                        debug.append(f"MENU-MINE {nm}")

        # ── 3.5) ⭐ LANDING-PARSE: ho sakta hai day-wise grid summary
        #    page ke ANDAR hi ho (user ne bhi yahi page bataya tha -
        #    "ye attendance page hai, ise scrape karo"). Landing pages
        #    ko khud parse karo - pehli baar ye step hai!
        for final_url, raw_text, _ls in landed:
            pshort = final_url.split("/")[-1].split("?")[0][:30]
            try:
                psoup = BeautifulSoup(raw_text, "html.parser")
            except Exception:
                continue
            pdays, pstats, ptot = self._parse_daily_attendance_page(psoup)
            debug.append(
                f"PAGE-PARSE [{pshort}] days={len(pdays)} entries={ptot}"
            )
            if pdays:
                ptitle = ""
                try:
                    if _ls.title and _ls.title.string:
                        ptitle = _ls.title.string.strip()
                except Exception:
                    pass
                result.update({
                    "success": True,
                    "found": True,
                    "days": pdays,
                    "stats": pstats,
                    "records": ptot,
                    "source_url": str(final_url),
                    "page_title": ptitle,
                })
                break

        debug.append(
            "ASPX-INDEX "
            + (", ".join(sorted(aspx_index)[:80])
               or "(koi bhi .aspx naam nahi mila)")
        )

        # ── 4) Candidates priority: CTX-URL (EncryptCode drill - sabse
        #    bharosemand) > LINK > MINED-URL > MINED(+token variants) >
        #    SIBLING-GUESS (asli page ke naam-cousins) > CLASSIC-GUESS.
        #    BARE urls sirf tab jab koi token hi na ho - ye portal
        #    token-less pe 404 (error.html) deta hai, bare tries waste hain.
        candidates = []
        seen = set()

        def _push(url):
            key = url.lower()
            if key not in seen:
                seen.add(key)
                candidates.append(url)

        def _detailish(u):
            fl = u.lower().replace("-", "").replace("_", "")
            return any(
                w in fl for w in ("detail", "daywise", "datewise", "coursewise")
            )

        for url in ctx_urls:
            _push(url)
        for url in link_urls + mined_urls:
            _push(url)
        for name in mined_names:
            if enc:
                _push(name + "?type=" + enc)
                if _detailish(name):
                    _push(name + "?code=" + enc)
            _push(name + "?type=" + _UIMS_STATIC_TOKEN)
            if not enc:
                _push(name)
        for p in self._DAILY_SIBLING_GUESSES:
            base = self.auth_url + p
            if enc:
                _push(base + "?type=" + enc)
                _push(base + "?code=" + enc)
            _push(base + "?type=" + _UIMS_STATIC_TOKEN)
        for p in self._DAILY_GUESS_PATHS:
            guess = self.auth_url + p
            if enc:
                _push(guess + "?type=" + enc)
            _push(guess + "?type=" + _UIMS_STATIC_TOKEN)
            if not enc:
                _push(guess)

        for i, cand in enumerate(candidates[:12], 1):
            debug.append(f"TRYLIST {i}) {cand}")
        # ⭐ Landing-parse me mil gaya to TRY phase poora skip
        ordered = [] if result["found"] else candidates[:8]
        # (404 is portal pe fast aata hai; hunt fail ho to 12h
        # campus-cache baaki sab skip karwa deta hai)

        for url in ordered:
            if time.time() > deadline:
                debug.append("DEADLINE 40s - baaki TRY skip")
                break
            try:
                resp = self.session.get(url, timeout=6, allow_redirects=True)
            except Exception as exc:
                debug.append(f"TRY {url} -> error {exc}")
                continue
            title = ""
            try:
                soup = BeautifulSoup(resp.text, "html.parser")
                if soup.title and soup.title.string:
                    title = soup.title.string.strip()
            except Exception:
                debug.append(f"TRY {url} -> soup error")
                continue
            debug.append(
                f"TRY {url} -> {resp.status_code} len={len(resp.text)} "
                f"title={title[:60]!r}"
            )
            if (
                "login" in resp.url.lower()
                or "txtloginpassword" in resp.text.lower()
            ):
                debug.append("LOGIN-REDIRECT detected; aage attempts band.")
                break
            if "error" in resp.url.lower() or resp.status_code == 404:
                # ⭐ Token-less/wrong page pe ye portal error.html deta hai
                debug.append("  404/error page - agla candidate")
                continue
            if resp.status_code == 200:
                portal_ok = True
            days, stats, total = self._parse_daily_attendance_page(soup)
            debug.append(f"  parsed days={len(days)} entries={total}")
            if days:
                result.update({
                    "success": True,
                    "found": True,
                    "days": days,
                    "stats": stats,
                    "records": total,
                    "source_url": str(resp.url),
                    "page_title": title,
                })
                break

        # ── 5) ⭐ PAGEMETHOD PROBES (AJAX): course-wise records khud isi
        #    page ke /GetReport webmethod se aate hain ({UID:'..',Session:
        #    '..'} body) - day-wise detail bhi kisi SIBLING method me
        #    hogi. Same handshake keys reuse karke chhote JSON POSTs.
        if not result["found"]:
            smatch = re.search(
                r"CurrentSession\s*\(\s*['\"]?([^'\")]+?)['\"]?\s*\)",
                summary_raw,
            )
            rmatch = re.search(
                r"getReport\s*\(\s*['\"]([^'\"]+?)['\"]", summary_raw
            )
            if not (summary_raw and smatch and rmatch):
                debug.append(
                    "PROBE skip - CurrentSession/getReport handshake keys "
                    "summary page pe nahi mili"
                )
            else:
                sess_k, rep_k = smatch.group(1), rmatch.group(1)
                endpoint = (
                    self.auth_url
                    + "frmStudentCourseWiseAttendanceSummary.aspx"
                )
                probe_names = []

                def _probe_push(m):
                    if (
                        m
                        and m.lower() != "getreport"
                        and m.lower() not in {x.lower() for x in probe_names}
                    ):
                        probe_names.append(m)

                for m in svc_methods:   # page ke asli inline endpoints pehle
                    _probe_push(m)
                for m in fn_methods:    # getXxx('...') se derived naams
                    _probe_push(m)
                for m in (              # phir likely sibling names
                    "GetDayWiseAttendance",
                    "GetAttendanceDetail",
                    "GetCourseWiseAttendanceDetail",
                    "GetStudentAttendanceDetail",
                    "GetDayWiseReport",
                    "GetDetailedReport",
                ):
                    _probe_push(m)
                probe_headers = {
                    "Content-Type": "application/json",
                    "Referer": endpoint + "?type=" + _UIMS_STATIC_TOKEN,
                }
                for name in probe_names[:6]:
                    if time.time() > deadline:
                        debug.append("DEADLINE 40s - baaki PROBE skip")
                        break
                    body = "{UID:'%s',Session:'%s'}" % (rep_k, sess_k)
                    try:
                        presp = self.session.post(
                            endpoint + "/" + name,
                            headers=probe_headers,
                            data=body,
                            timeout=8,
                        )
                    except Exception as exc:
                        debug.append(f"PROBE {name} -> error {exc}")
                        continue
                    debug.append(
                        f"PROBE {name} -> {presp.status_code} "
                        f"len={len(presp.text)}"
                    )
                    if presp.status_code != 200 or not presp.text.strip():
                        continue
                    try:
                        wrapper = presp.json()
                        data = wrapper.get("d") if isinstance(wrapper, dict) else None
                        if isinstance(data, str):
                            data = json.loads(data)
                    except Exception:
                        debug.append(f"  PROBE {name} - response JSON nahi")
                        continue
                    recs = data if isinstance(data, list) else (
                        data.get("records")
                        if isinstance(data, dict)
                        else None
                    )
                    if not recs or not isinstance(recs, list):
                        debug.append(f"  PROBE {name} records nahi (type)"
                                     )
                        continue
                    if isinstance(recs[0], dict):
                        debug.append(
                            f"  PROBE-KEYS {name} records={len(recs)} "
                            f"keys={sorted(str(k) for k in recs[0].keys())[:14]}"
                        )
                    entries = self._daily_from_records(recs)
                    debug.append(
                        f"  PROBE {name} mapped-entries={len(entries)}"
                    )
                    if entries:
                        days, stats, total = self._daily_build_days(entries)
                        if days:
                            result.update({
                                "success": True,
                                "found": True,
                                "days": days,
                                "stats": stats,
                                "records": total,
                                "source_url": endpoint + "/" + name,
                                "page_title": f"AJAX {name}",
                            })
                            break

        # ── 6) ⭐ TABLE-DUMP: phir bhi kuch nahi mila to summary landing
        #    ke tables ka skeleton debug me - hidden day-wise grid /
        #    ReportViewer ka asli structure yahin se pakda jaayega.
        if not result["found"] and summary_raw:
            try:
                dsoup = BeautifulSoup(summary_raw, "html.parser")
                dtables = dsoup.find_all("table")
                debug.append(f"TABLES on summary page: {len(dtables)}")
                for ti, dtable in enumerate(dtables[:6]):
                    drows = dtable.find_all("tr")
                    tid = dtable.get("id") or ""
                    head = []
                    if drows:
                        head = [
                            re.sub(r"\s+", " ", c.get_text(" ", strip=True))[:22]
                            for c in drows[0].find_all(["th", "td"],
                                                       recursive=False)
                        ]
                    debug.append(
                        f"TABLE{ti} id={tid[:30]!r} rows={len(drows)} "
                        f"head={head[:8]}"
                    )
                    for drow in drows[1:3]:
                        dcells = [
                            re.sub(r"\s+", " ", c.get_text(" ", strip=True))[:22]
                            for c in drow.find_all(["th", "td"],
                                                   recursive=False)
                        ]
                        if any(dcells):
                            debug.append(f"  ROW {dcells[:8]}")
                for ifr in dsoup.find_all("iframe", src=True):
                    debug.append(f"IFRAME {ifr['src'][:90]}")
                if "ReportViewer" in summary_raw:
                    rv_i = summary_raw.find("ReportViewer")
                    hint = re.sub(
                        r"\s+", " ",
                        summary_raw[max(0, rv_i - 120): rv_i + 120],
                    )
                    debug.append(f"REPORTVIEWER ...{hint[:200]}...")
            except Exception:
                pass

        # ── 7) ⭐ RAW-DUMP DIAGNOSTICS (v5.1): live ASPX-INDEX ne prove
        #    kar diya ki is campus pe day-wise ka ALAG PAGE exist hi nahi
        #    karta (saare classic names 404, guesses 500). SortTable AJAX
        #    se bharti hai aur detail #fullreport me aati hai - ab poore
        #    raaste dump karo taaki exact wiring ho sake:
        #    a) GetReport ka RAW JSON (ho sakta hai date-wise detail isi
        #       response ke andar ho - abhi hum sirf summary uthate hain)
        #    b) summary page ka POORA HTML (inline JS me #fullreport fill
        #       karne wala detail webmethod + params likha hoga)
        #    c) sitemap ke 2 asli suspects: DailyDiary + WinningCamp
        # ⭐ v5.2: RAW GetReport se per-course EncryptCodes + Title yahan
        #    collect hote hain - DETAIL SNIFFER (section 8) isi se har
        #    course ka day-wise modal fetch karega.
        _report_courses = []
        if not result["found"]:
            try:
                _rep_ep = (
                    self.auth_url
                    + "frmStudentCourseWiseAttendanceSummary.aspx"
                )
                _sm2 = re.search(
                    r"CurrentSession\s*\(\s*['\"]?([^'\")]+?)['\"]?\s*\)",
                    summary_raw,
                )
                _rm2 = re.search(
                    r"getReport\s*\(\s*['\"]([^'\"]+?)['\"]",
                    summary_raw,
                )
                if summary_raw and _sm2 and _rm2:
                    _presp = self.session.post(
                        _rep_ep + "/GetReport",
                        headers={
                            "Content-Type": "application/json",
                            "Referer": (
                                _rep_ep + "?type=" + _UIMS_STATIC_TOKEN
                            ),
                        },
                        data="{UID:'%s',Session:'%s'}" % (
                            _rm2.group(1),
                            _sm2.group(1),
                        ),
                        timeout=10,
                    )
                    _rt = _presp.text or ""
                    debug.append(
                        f"RAWREPORT GetReport -> {_presp.status_code} "
                        f"len={len(_rt)}"
                    )
                    if _presp.status_code == 200 and _rt.strip():
                        _rj = None
                        try:
                            _wrap = _presp.json()
                            _rj = _wrap.get("d") if isinstance(
                                _wrap, dict
                            ) else None
                            if isinstance(_rj, str):
                                _rj = json.loads(_rj)
                        except Exception:
                            _rj = None
                        if isinstance(_rj, list) and _rj and isinstance(
                            _rj[0], dict
                        ):
                            _rk = sorted(str(k) for k in _rj[0].keys())
                            debug.append(
                                f"RAWREPORT records={len(_rj)} keys={_rk}"
                            )
                            # ⭐ v5.2: har course ka EncryptCode + Title
                            # stash - detail fetch ke liye.
                            for _cr in _rj:
                                if not isinstance(_cr, dict):
                                    continue
                                _ce = str(
                                    _cr.get("EncryptCode") or ""
                                ).strip()
                                if _ce and all(
                                    c["enc"] != _ce
                                    for c in _report_courses
                                ):
                                    _report_courses.append({
                                        "code": str(
                                            _cr.get("Code") or ""
                                        ).strip().upper(),
                                        "enc": _ce,
                                        "title": str(
                                            _cr.get("Title") or ""
                                        ).strip(),
                                    })
                            debug.append(
                                f"RAWREPORT course-codes="
                                f"{len(_report_courses)}"
                            )
                            _sample = {
                                k: str(_rj[0].get(k))[:36]
                                for k in _rk[:12]
                            }
                            debug.append(f"RAWREPORT rec0 {_sample}")
                        else:
                            debug.append(
                                "RAWREPORT records-parse nahi hua "
                                "(raw file dekho)"
                            )
                        try:
                            with open(
                                "attendance_getreport_raw.txt",
                                "w",
                                encoding="utf-8",
                            ) as fh:
                                fh.write(_rt[:40000])
                            debug.append(
                                "RAWREPORT dump -> "
                                "attendance_getreport_raw.txt"
                            )
                        except OSError:
                            pass
                else:
                    debug.append(
                        "RAWREPORT skip - summary keys nahi mili"
                    )

                # b) POORA page HTML - detail webmethod page ke inline JS
                #    me likha hai (kaun #fullreport bharta hai, kis call se)
                if summary_raw:
                    try:
                        with open(
                            "attendance_summary_page.html",
                            "w",
                            encoding="utf-8",
                        ) as fh:
                            fh.write(summary_raw[:250000])
                        debug.append(
                            f"PAGE-DUMP -> attendance_summary_page.html "
                            f"len={len(summary_raw)}"
                        )
                    except OSError:
                        pass
                    _fr_seen = 0
                    _fr_idx = summary_raw.find("fullreport")
                    while _fr_idx >= 0 and _fr_seen < 3:
                        _fr_seen += 1
                        _fr_ctx = re.sub(
                            r"\s+",
                            " ",
                            summary_raw[
                                max(0, _fr_idx - 300): _fr_idx + 300
                            ],
                        )
                        debug.append(
                            f"FULLREPORT-CTX{_fr_seen} ...{_fr_ctx[:560]}..."
                        )
                        _fr_idx = summary_raw.find(
                            "fullreport", _fr_idx + 10
                        )
                else:
                    debug.append("PAGE-DUMP skip - summary_raw khaali")
            except Exception as exc:
                debug.append(f"RAW-DUMP error {exc}")

            # c) ⭐ SITEMAP SUSPECTS: ASPX-INDEX ke 2 asli pages jo
            #    classic guesses me nahi the - roj-ka log inme ho sakta.
            if time.time() <= deadline:
                for _sus in (
                    "frmStudentDailyDiary.aspx",
                    "frmStudentWinningCampAttendanceSummary.aspx",
                ):
                    _sus_tokens = [_UIMS_STATIC_TOKEN]
                    if enc and enc != _UIMS_STATIC_TOKEN:
                        _sus_tokens.append(enc)
                    for _stok in _sus_tokens:
                        _surl = self.auth_url + _sus + "?type=" + _stok
                        try:
                            _sresp = self.session.get(
                                _surl, timeout=6, allow_redirects=True
                            )
                        except Exception as exc:
                            debug.append(f"TRY {_sus} -> error {exc}")
                            continue
                        _stitle = ""
                        try:
                            _ssoup = BeautifulSoup(
                                _sresp.text, "html.parser"
                            )
                            if _ssoup.title and _ssoup.title.string:
                                _stitle = _ssoup.title.string.strip()
                        except Exception:
                            continue
                        debug.append(
                            f"TRY {_sus} -> {_sresp.status_code} "
                            f"len={len(_sresp.text)} "
                            f"title={_stitle[:60]!r}"
                        )
                        if (
                            "login" in _sresp.url.lower()
                            or "txtloginpassword" in _sresp.text.lower()
                        ):
                            break
                        if (
                            "error" in _sresp.url.lower()
                            or _sresp.status_code == 404
                        ):
                            break  # is page ka agla token faaltu hai
                        if _sresp.status_code == 200:
                            portal_ok = True
                            _sdays, _sstats, _stotal = (
                                self._parse_daily_attendance_page(_ssoup)
                            )
                            debug.append(
                                f"  parsed days={len(_sdays)} "
                                f"entries={_stotal}"
                            )
                            if _sdays:
                                result.update({
                                    "success": True,
                                    "found": True,
                                    "days": _sdays,
                                    "stats": _sstats,
                                    "records": _stotal,
                                    "source_url": str(_sresp.url),
                                    "page_title": _stitle,
                                })
                                break
                    if result["found"]:
                        break

        # ── 8) ⭐ GETFULLREPORT DETAIL-WIRING (v5.5 - JS contract + auto
        #    body-variants): protocol portal ke JS se confirm:
        #      POST .../GetFullReport {course,UID,fromDate,toDate,type,
        #      Session} -> {"d":{"Result":"<json-list>"}}
        #    Live: har course 200 par len=167 (= server ka "No Data
        #    Found" jaisa chhota reply) = METHOD SAHI, params me se koi
        #    value alag hai (row-onclick real values apne binding-loop
        #    se bhejta hai). Har course ke liye 4 realistic variants
        #    order me try - pehla jo rows de wo use, baaki skip.
        #    Chhote responses (<=400 chars) ka CONTENT console pe print -
        #    server ka message (missing param / no data) seedha dikhta.
        if not result["found"]:
            try:
                _rep_ep = (
                    self.auth_url
                    + "frmStudentCourseWiseAttendanceSummary.aspx"
                )
                _rep_url = _rep_ep + "?type=" + _UIMS_STATIC_TOKEN
                _sm3 = re.search(
                    r"CurrentSession\s*\(\s*['\"]?([^'\")]+?)['\"]?\s*\)",
                    summary_raw,
                )
                _rm3 = re.search(
                    r"getReport\s*\(\s*['\"]([^'\"]+?)['\"]",
                    summary_raw,
                )
                _df_courses = list(_report_courses)
                for _ec in enc_codes:  # views ke tokens bhi shamil
                    if _ec and all(
                        c["enc"] != _ec for c in _df_courses
                    ):
                        _df_courses.append(
                            {"code": "", "enc": _ec, "title": ""}
                        )

                _all_entries = []
                _best_dump = ""
                # ⭐ v5.6 SUBJECT-DRILLDOWN: views ke stray enc-token
                # (code-less '?') wahi rows duplicate karta tha jo coded
                # course la chuka - cross-course dedupe zaroori. Aur
                # per-course entries alag collect karo taaki ATTENDANCE
                # tab me course card click pe USI subject ka P/A log
                # khul sake (user request).
                _seen_global = set()
                _subj_entries = []
                _TYPE_MAP = {
                    "L": "Lecture",
                    "P": "Practical",
                    "T": "Tutorial",
                }
                _STATUS_MAP = {"P": "Present", "A": "Absent"}

                if not (_sm3 and _rm3 and _df_courses):
                    debug.append(
                        "DETAIL-FETCH skip - keys/courses nahi mile "
                        f"(courses={len(_df_courses)})"
                    )
                else:
                    debug.append(
                        f"DETAIL-FETCH courses={len(_df_courses)} "
                        "via GetFullReport (JS contract + variants)"
                    )
                    for _c in _df_courses[:10]:
                        if time.time() > deadline:
                            debug.append(
                                "DEADLINE 40s - baaki courses yahin roki"
                            )
                            break
                        _clabel = _c["code"] or "?"
                        _plain = _c["code"] or ""
                        _enc = _c["enc"]
                        _ur = _rm3.group(1)
                        _us = _sm3.group(1)
                        _up = str(getattr(self, "uid", "") or "")
                        # ⭐ PROVEN-FIRST (v5.6): live log ne SABIT kar
                        # diya - sirf enc+dates HIT karta hai (fromDate/
                        # toDate bhare hone chahiye), baaki 3 empty-date
                        # variants hamesha 'No Data Found' dete hain. To
                        # dates wala PEHLE - 9 courses x 3 waste calls
                        # bache (27 extra round-trips), sync 3x faster.
                        # Purane variants fallback ke liye rakhe hain.
                        _variants = [
                            (
                                "enc+dates",
                                "{course:'%s',UID:'%s',fromDate:'01 Jan 2026',"
                                "toDate:'31 Dec 2026',type:'',Session:'%s'}"
                                % (_enc, _ur, _us),
                            ),
                            (
                                "enc+encU",
                                "{course:'%s',UID:'%s',fromDate:'',"
                                "toDate:'',type:'',Session:'%s'}"
                                % (_enc, _ur, _us),
                            ),
                        ]
                        if _up and _up != _ur:
                            _variants.append((
                                "enc+plainU",
                                "{course:'%s',UID:'%s',fromDate:'',"
                                "toDate:'',type:'',Session:'%s'}"
                                % (_enc, _up, _us),
                            ))
                        if _plain:
                            _variants.append((
                                "code+encU",
                                "{course:'%s',UID:'%s',fromDate:'',"
                                "toDate:'',type:'',Session:'%s'}"
                                % (_plain, _ur, _us),
                            ))

                        _course_got = 0
                        _seen_rows = set()
                        for _vlab, _body in _variants:
                            if _course_got:
                                break  # pichla variant HIT - aage skip
                            try:
                                _pr = self.session.post(
                                    _rep_ep + "/GetFullReport",
                                    headers={
                                        "Content-Type": (
                                            "application/json; "
                                            "charset=utf-8"
                                        ),
                                        "Referer": _rep_url,
                                    },
                                    data=_body,
                                    timeout=10,
                                )
                            except Exception as exc:
                                debug.append(
                                    f"DETAIL {_clabel}/{_vlab} -> "
                                    f"error {exc}"
                                )
                                continue
                            _ptxt = _pr.text or ""
                            if len(_ptxt) <= 400:
                                # ⭐ Chhota reply = info-message - content
                                # console pe daal do (missing param / no
                                # data seedha dikhega).
                                _sn = " ".join(_ptxt.split())
                                print(
                                    f"[DailyAtt] DETAIL {_clabel}/{_vlab}"
                                    f" -> {_pr.status_code} "
                                    f"len={len(_ptxt)} body='{_sn[:350]}'"
                                )
                                debug.append(
                                    f"DETAIL {_clabel}/{_vlab} -> "
                                    f"{_pr.status_code} len={len(_ptxt)} "
                                    f"body='{_sn[:350]}'"
                                )
                            else:
                                print(
                                    f"[DailyAtt] DETAIL {_clabel}/{_vlab}"
                                    f" -> {_pr.status_code} "
                                    f"len={len(_ptxt)}"
                                )
                                debug.append(
                                    f"DETAIL {_clabel}/{_vlab} -> "
                                    f"{_pr.status_code} len={len(_ptxt)}"
                                )
                            if _pr.status_code != 200 or not _ptxt.strip():
                                continue
                            if len(_ptxt) > len(_best_dump):
                                _best_dump = _ptxt[:60000]

                            _rows = None
                            _nodata = False
                            try:
                                _wrap = _pr.json()
                                _dd = (
                                    _wrap.get("d")
                                    if isinstance(_wrap, dict)
                                    else None
                                )
                                _raw = (
                                    _dd.get("Result")
                                    if isinstance(_dd, dict)
                                    else _dd
                                )
                                if isinstance(_raw, str):
                                    if _raw.strip() == "No Data Found":
                                        _nodata = True
                                    else:
                                        _rows = json.loads(_raw)
                                elif isinstance(_raw, list):
                                    _rows = _raw
                            except Exception:
                                debug.append(
                                    f"DETAIL {_clabel}/{_vlab} "
                                    "JSON-parse fail"
                                )
                                continue
                            if _nodata or not isinstance(_rows, list):
                                continue

                            _got = 0
                            for _r in _rows:
                                if not isinstance(_r, dict):
                                    continue
                                _attdate = str(_r.get("AttDate") or "")
                                _mdate = self._DAILY_DATE_RE.search(
                                    _attdate
                                )
                                if not _mdate:
                                    continue
                                _acd = str(
                                    _r.get("AttendanceCode") or ""
                                ).strip()
                                _status = _STATUS_MAP.get(
                                    _acd.upper(), _acd
                                )
                                if (
                                    not _status
                                    or not self._DAILY_STATUS_RE.match(
                                        _status
                                    )
                                ):
                                    continue
                                _tcode = (
                                    str(_r.get("AttendanceType") or "")
                                    .strip()
                                    .upper()
                                )
                                _typ = _TYPE_MAP.get(_tcode, _tcode)
                                _timing = str(_r.get("Timing") or "")[:20]
                                _rkey = (
                                    _mdate.group(0),
                                    _timing,
                                    _c["code"],
                                    _status,
                                    _typ,
                                )
                                if _rkey in _seen_rows:
                                    continue
                                _seen_rows.add(_rkey)
                                # ⭐ v5.6: cross-course duplicate kill -
                                # views ke stray enc-token ('?' course)
                                # wahi (date,time,status,type) row dobara
                                # laata hai jo asli coded course de
                                # chuka (live: 42 real + 1 dupe = 43).
                                _gkey = (
                                    _mdate.group(0),
                                    _timing,
                                    _status,
                                    _typ,
                                )
                                if _gkey in _seen_global:
                                    continue
                                _seen_global.add(_gkey)
                                _got += 1
                                _ttl = (
                                    _c["title"] or _c["code"] or "Lecture"
                                )
                                if (
                                    _typ
                                    and _typ.lower() not in _ttl.lower()
                                ):
                                    _ttl = f"{_ttl} ({_typ})"
                                # ⭐ v5.7: Timeline view ke liye din ka
                                # naam ("Thursday,") + marked-by (faculty)
                                _wdm = re.match(
                                    r"\s*([A-Za-z]+)\s*,", _attdate
                                )
                                _wday = (
                                    _wdm.group(1) if _wdm else ""
                                )[:9]
                                if not _wday:
                                    _dtk = self._daily_date_sortkey(
                                        _mdate.group(0)
                                    )
                                    _wday = (
                                        _dtk.strftime("%A") if _dtk else ""
                                    )
                                _by = str(_r.get("Name") or "")[:50]
                                _all_entries.append({
                                    "date": _mdate.group(0).strip()[:18],
                                    "wday": _wday,
                                    "time": _timing,
                                    "code": (_c["code"] or "")[:16],
                                    "title": _ttl.strip()[:70],
                                    "typ": (_typ or "")[:12],
                                    "by": _by,
                                    "status": _status.upper()[:10],
                                    "tone": self._daily_tone(_status),
                                })
                            if _got:
                                _course_got += _got
                                print(
                                    f"[DailyAtt] DETAIL-HIT {_clabel}"
                                    f"/{_vlab} rows={len(_rows)} "
                                    f"entries={_got}"
                                )
                                debug.append(
                                    f"DETAIL-HIT {_clabel}/{_vlab} "
                                    f"rows={len(_rows)} entries={_got}"
                                )
                        if not _course_got:
                            debug.append(
                                f"DETAIL {_clabel} - koi variant kaam "
                                "nahi (No Data)"
                            )

                if _best_dump:
                    try:
                        with open(
                            "attendance_detail_try.txt",
                            "w",
                            encoding="utf-8",
                        ) as fh:
                            fh.write(_best_dump)
                        debug.append(
                            "DETAIL-DUMP -> attendance_detail_try.txt"
                        )
                    except OSError:
                        pass

                if _all_entries:
                    days, stats, total = self._daily_build_days(_all_entries)
                    debug.append(
                        f"DETAIL-FETCH total entries={len(_all_entries)} "
                        f"days={len(days)}"
                    )
                    # ⭐ v5.6 SUBJECT-DRILLDOWN: flat entries ko course
                    # code se group karo - ATTENDANCE tab me jis subject
                    # pe click karo, usi ka date-wise P/A khule. Stray
                    # '?' (code-less) entries sirf day-log me rehti hain.
                    _subjects = []
                    _sorder = []
                    _smap = {}
                    for _e in _all_entries:
                        _sc = (_e.get("code") or "").strip()
                        if not _sc:
                            continue
                        if _sc not in _smap:
                            _bt = re.sub(
                                r"\s*\((Lecture|Practical|Tutorial)\)\s*$",
                                "",
                                _e.get("title") or "",
                            ).strip()
                            _smap[_sc] = {
                                "code": _sc,
                                "title": (_bt or _sc)[:60],
                                "present": 0,
                                "absent": 0,
                                "pct": 0,
                                "entries": [],
                            }
                            _sorder.append(_sc)
                        _srec = _smap[_sc]
                        if _e["tone"] == "present":
                            _srec["present"] += 1
                        elif _e["tone"] == "absent":
                            _srec["absent"] += 1
                        _srec["entries"].append({
                            "date": _e["date"],
                            "wday": _e.get("wday", ""),
                            "time": _e.get("time", ""),
                            "typ": _e.get("typ", ""),
                            "by": _e.get("by", ""),
                            "status": _e["status"],
                            "tone": _e["tone"],
                        })
                    for _sc in _sorder:
                        _srec = _smap[_sc]
                        # newest pehle; same date pe portal order stable
                        _srec["entries"].sort(
                            key=lambda x: (
                                self._daily_date_sortkey(x["date"])
                                or _dt_mod.date.min
                            ),
                            reverse=True,
                        )
                        _sp, _sa = _srec["present"], _srec["absent"]
                        _srec["pct"] = (
                            round(_sp * 100 / (_sp + _sa))
                            if (_sp + _sa) else 0
                        )
                        _srec["entries"] = _srec["entries"][:40]
                        _subjects.append(_srec)
                    debug.append(
                        f"SUBJECTS built={len(_subjects)} "
                        f"({', '.join(_sorder[:5])})"
                    )
                    if days:
                        result.update({
                            "success": True,
                            "found": True,
                            "days": days,
                            "subjects": _subjects,
                            "stats": stats,
                            "records": total,
                            "source_url": _rep_ep + "/GetFullReport",
                            "page_title": "Course Detail (GetFullReport)",
                        })
                        print(
                            f"[DailyAtt] DETAIL-FETCH OK entries="
                            f"{len(_all_entries)} days={len(days)} "
                            f"subjects={len(_subjects)}"
                        )
            except Exception as exc:
                debug.append(f"DETAIL-FETCH error {exc}")

        # ⭐ Campus no-page cache update: portal ZINDA tha phir bhi kuch
        # nahi mila = page sach me nahi hai -> 12h skip. Page MIL gaya to
        # purana marker clear (campus pe baad me module ON hua to bhi chale).
        if result["found"]:
            _DAILY_NO_PAGE.pop(cache_key, None)
        elif portal_ok:
            _DAILY_NO_PAGE[cache_key] = time.time()
            debug.append(
                "CAMPUS-CACHE set: is campus ka day-wise page nahi mila "
                "(12h tak hunt skip - login fast rahega)"
            )

        debug.append(
            f"RESULT found={result['found']} days={len(result['days'])} "
            f"records={result['records']}"
        )
        if not result["found"]:
            debug.append(
                "HINT day-wise nahi mila - upar PROBE-KEYS / PAGE-PARSE / "
                "TABLE / FN / SVC / CTX-URL lines me asli raasta hoga "
                "(ASPX-INDEX = portal ka poora sitemap). Ye file bhej do, "
                "main exact wire kar dunga."
            )
        try:
            with open("attendance_daily_debug.txt", "w", encoding="utf-8") as fh:
                fh.write("\n".join(debug))
        except OSError:
            pass

        print(
            f"[DailyAtt] found={result['found']} days={len(result['days'])} "
            f"records={result['records']} enc={'yes' if enc else 'no'} "
            f"src={result['source_url'] or '-'}"
        )
        return result

    def _parse_daily_attendance_page(self, soup):
        """Day-wise page se P/A entries -> grouped days + stats.

        Returns (days, stats, total). days newest-pehle, har item:
        {"label","weekday","p","a","items":[{time,code,title,status,tone}]}
        Sirf last 15 days (UI fast + saaf rakhta hai).
        """
        for tag in soup.find_all(["script", "style", "noscript", "link"]):
            tag.decompose()

        entries = []
        seen_e = set()

        def emit(date_raw, time_txt, code, title, status):
            status = str(status).strip().upper()[:10]
            key = (
                str(date_raw).strip()[:18],
                (time_txt or "")[:20],
                (code or "")[:16],
                status,
            )
            if not key[0] or not status or key in seen_e:
                return
            seen_e.add(key)
            # Matrix/blob cells me title ke saath course-code chipka ho
            # sakta hai ("Cloud Security 25CST-207") - title saaf karo.
            clean_title = self._COURSE_CODE_RE.sub("", title or "").strip(" :-–—|")
            entries.append({
                "date": key[0],
                "time": key[1],
                "code": key[2],
                "title": (clean_title or title or code or "Lecture").strip()[:70],
                "status": status,
                "tone": self._daily_tone(status),
            })

        tables = soup.find_all("table")

        # ── 1) MATRIX form: header row me 2+ dates, niche P/A cells ──
        for table in tables:
            rows = []
            for tr in table.find_all("tr"):
                cells = [
                    re.sub(r"\s+", " ", c.get_text(" ", strip=True))
                    for c in tr.find_all(["th", "td"], recursive=False)
                ]
                rows.append(cells)
            if len(rows) < 2:
                continue
            header_idx, date_cols = -1, []
            for ri, cells in enumerate(rows[:4]):
                cols = [
                    ci for ci, c in enumerate(cells)
                    if self._DAILY_DATE_RE.search(c or "")
                ]
                if len(cols) >= 2:
                    header_idx, date_cols = ri, cols
                    break
            if header_idx < 0:
                continue
            header = rows[header_idx]
            for cells in rows[header_idx + 1:]:
                if not any(cells):
                    continue
                code = ""
                code_m = self._COURSE_CODE_RE.search(" ".join(cells))
                if code_m:
                    code = code_m.group(1)
                title = ""
                for ci, c in enumerate(cells):
                    if ci in date_cols or not c:
                        continue
                    if self._DAILY_STATUS_RE.match(c) or self._DAILY_DATE_RE.search(c):
                        continue
                    if self._COURSE_CODE_RE.fullmatch(c.strip()):
                        continue
                    if (len(c) >= 6 and re.search(r"[A-Za-z]{3}", c)
                            and len(c) > len(title)):
                        title = c
                for ci in date_cols:
                    if ci >= len(cells):
                        continue
                    cell = (cells[ci] or "").strip()
                    if cell and self._DAILY_STATUS_RE.match(cell):
                        emit(header[ci], "", code, title, cell)
            if entries:
                break

        # ── 2) ROW form: har row me Date + Status ──
        if not entries:
            for table in tables:
                for tr in table.find_all("tr"):
                    cells = [
                        re.sub(r"\s+", " ", c.get_text(" ", strip=True))
                        for c in tr.find_all(["th", "td"], recursive=False)
                    ]
                    cells = [c for c in cells if c]
                    if len(cells) < 2:
                        continue
                    date_raw = ""
                    for c in cells:
                        m = self._DAILY_DATE_RE.search(c)
                        if m:
                            date_raw = m.group(0)
                            break
                    if not date_raw:
                        continue
                    status = next(
                        (c for c in cells if self._DAILY_STATUS_RE.match(c)), ""
                    )
                    if not status:
                        continue
                    time_txt = ""
                    for c in cells:
                        if self._DAILY_DATE_RE.search(c):
                            continue
                        m = self._DAILY_TIME_RE.search(c)
                        if m:
                            time_txt = m.group(0)
                            break
                    code = ""
                    code_m = self._COURSE_CODE_RE.search(" ".join(cells))
                    if code_m:
                        code = code_m.group(1)
                    title = ""
                    for c in cells:
                        if c == date_raw or self._DAILY_STATUS_RE.match(c):
                            continue
                        if self._DAILY_TIME_RE.fullmatch(c.strip()):
                            continue
                        if self._COURSE_CODE_RE.fullmatch(c.strip()):
                            continue
                        if (len(c) >= 6 and re.search(r"[A-Za-z]{3}", c)
                                and len(c) > len(title)):
                            title = c
                    emit(date_raw, time_txt, code, title, status)

        # ── group by date (newest pehle) - shared builder ──
        return self._daily_build_days(entries)

    def _daily_date_sortkey(self, raw):
        """⭐ '30 Jul 2026' jaise date-string -> sortable date (na bane
        to None). _daily_build_days ka parser hi hai - subject-drilldown
        ke newest-first sort ke liye alag helper (nested date_key ko
        touch nahi kiya, proven path as-is)."""
        clean = re.sub(r"\s+", " ", str(raw).replace(",", "")).strip()
        for fmt in ("%d %b %Y", "%d %B %Y", "%d %b %y",
                    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y",
                    "%d-%m-%y", "%d/%m/%y", "%d.%m.%Y", "%d.%m.%y"):
            try:
                return _dt_mod.datetime.strptime(clean, fmt).date()
            except ValueError:
                continue
        today = _dt_mod.date.today()
        for fmt in ("%d %b", "%b %d"):
            try:
                guess = _dt_mod.datetime.strptime(clean, fmt).date()
                guess = guess.replace(year=today.year)
                if (guess - today).days > 3:
                    guess = guess.replace(year=today.year - 1)
                return guess
            except ValueError:
                continue
        return None

    def _daily_build_days(self, entries):
        """entries (emit-style) -> grouped days + stats. ROW-form,
        MATRIX-form aur AJAX-records teeno yahin se guzarte hain."""
        def date_key(raw):
            clean = re.sub(r"\s+", " ", str(raw).replace(",", "")).strip()
            for fmt in ("%d %b %Y", "%d %B %Y", "%d %b %y",
                        "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y",
                        "%d-%m-%y", "%d/%m/%y", "%d.%m.%Y", "%d.%m.%y"):
                try:
                    return _dt_mod.datetime.strptime(clean, fmt).date()
                except ValueError:
                    continue
            # Year-less headers ("21 Jul" matrix grid): current year maan
            # lo; future me pada ho (Dec ka log, Jan me viewing) to pichla saal.
            today = _dt_mod.date.today()
            for fmt in ("%d %b", "%b %d"):
                try:
                    guess = _dt_mod.datetime.strptime(clean, fmt).date()
                    guess = guess.replace(year=today.year)
                    if (guess - today).days > 3:
                        guess = guess.replace(year=today.year - 1)
                    return guess
                except ValueError:
                    continue
            return None

        groups = {}
        for e in entries:
            groups.setdefault(date_key(e["date"]) or e["date"], []).append(e)

        real = [(k, v) for k, v in groups.items() if isinstance(k, _dt_mod.date)]
        other = [(k, v) for k, v in groups.items() if not isinstance(k, _dt_mod.date)]
        real.sort(key=lambda kv: kv[0], reverse=True)
        ordered = real + other

        days = []
        for k, items in ordered[:15]:
            p = sum(1 for i in items if i["tone"] == "present")
            a = sum(1 for i in items if i["tone"] == "absent")
            if isinstance(k, _dt_mod.date):
                label, weekday = k.strftime("%d %b"), k.strftime("%a")
            else:
                label, weekday = str(k)[:12], ""
            days.append({
                "label": label, "weekday": weekday,
                "p": p, "a": a, "items": items,
            })

        tp = sum(d["p"] for d in days)
        ta = sum(d["a"] for d in days)
        stats = {
            "present": tp,
            "absent": ta,
            "pct": round(tp * 100 / (tp + ta)) if (tp + ta) else 0,
        }
        return days, stats, len(entries)

    def _dailydetail_entries_from_html(self, html_text, titles=None):
        """⭐ Screenshot-wali detail-MODAL parser (v5.2).

        User ke portal screenshot ka exact layout:
          upar 'Attendance Summary' + UID/Semester/Name/'Course Code: X'
          neeche table: SrNo | Date | Type | Time | Attendance |
                        Section | Group | Marked By
          rows: 'Thursday, 30 Jul 2026 | Practical | 3:45 - 4:35 PM |
                 Present | ...'
        HTML-fragment (AJAX {d:"<table..."}) ya full-page dono chalte
        hain. `titles` = {"COURSE-CODE": "Title"} (GetReport se) - ho to
        asli subject-title lagte hain, nahi to Type/code fallback.
        Returns: emit-style entries (date/time/code/title/status/tone).
        """
        entries = []
        if not html_text or "<" not in html_text:
            return entries
        try:
            soup = BeautifulSoup(html_text, "html.parser")
        except Exception:
            return entries
        for tag in soup.find_all(["script", "style", "noscript"]):
            tag.decompose()

        page_text = soup.get_text(" ", strip=True)
        code = ""
        mcode = re.search(
            # ⭐ Asli CU codes DIGITS se shuru hote hain: 25CSH-219
            r"Course\s*Code\s*[:#]?\s*([A-Za-z0-9]{2,7}-[A-Za-z0-9]{1,6})",
            page_text,
        )
        if mcode:
            code = mcode.group(1).strip().upper()
        title_map = titles or {}
        title = title_map.get(code, "")
        if not title:  # fragment me code nahi tha to single-code map se
            if len(title_map) == 1:
                code = code or next(iter(title_map))
                title = title_map.get(code, "")

        date_re = self._DAILY_DATE_RE
        time_re = self._DAILY_TIME_RE
        status_re = self._DAILY_STATUS_RE

        def _colmap(header_texts, *needles):
            for i, t in enumerate(header_texts):
                if any(n in t for n in needles):
                    return i
            return None

        for table in soup.find_all("table"):
            trs = table.find_all("tr")
            if not trs:
                continue
            hdr = [
                h.get_text(" ", strip=True).lower()
                for h in trs[0].find_all(["th", "td"])
            ]
            i_date = _colmap(hdr, "date")
            i_att = _colmap(hdr, "attendance", "status", "presence")
            if i_date is None or i_att is None:
                continue
            i_time = _colmap(hdr, "time")
            i_type = _colmap(hdr, "type")

            seen_e = set()
            for tr in trs[1:]:
                cells = tr.find_all("td")

                def _ct(i):
                    if i is None or i >= len(cells):
                        return ""
                    return cells[i].get_text(" ", strip=True)

                raw_date = _ct(i_date)
                mdate = date_re.search(raw_date)
                if not mdate:
                    continue
                status = _ct(i_att).strip()
                if not status_re.match(status):
                    continue
                time_txt = _ct(i_time)
                mtime = time_re.search(time_txt)
                time_txt = (mtime.group(0) if mtime else time_txt)[:20]
                typ = _ct(i_type).strip()[:16]

                key = (mdate.group(0), time_txt, code, status.upper(), typ)
                if key in seen_e:
                    continue
                seen_e.add(key)

                # ⭐ Date me weekday chipka ho sakta hai ("Thursday, 30
                # Jul 2026") - DATE_RE ne sirf asli date uthaya hai.
                ttl = title or code or "Lecture"
                if typ and typ.lower() not in ttl.lower():
                    ttl = f"{ttl} ({typ})"
                entries.append({
                    "date": mdate.group(0).strip()[:18],
                    "time": time_txt,
                    "code": (code or "")[:16],
                    "title": ttl.strip()[:70],
                    "status": status.upper()[:10],
                    "tone": self._daily_tone(status),
                })
        return entries

    def _daily_from_records(self, records):
        """⭐ AJAX/PageMethod JSON records -> emit-style entries.

        Key names case-insensitive guess hote hain (Date*/LectureTime/
        Course/Status/IsPresent...). Status str (P/A/Present/Absent) ya
        bool/int (true/1 -> present) dono chalte hain. Jo record map na
        ho use chhod do - partial data bhi kaafi hai.
        """
        def _norm_status(v):
            if isinstance(v, bool):
                return "P" if v else "A"
            s = str(v).strip()
            sl = s.lower()
            if sl in ("1", "true", "yes", "y"):
                return "P"
            if sl in ("0", "false", "no", "n"):
                return "A"
            if self._DAILY_STATUS_RE.match(s):
                return s.upper()[:10]
            return ""

        entries = []
        seen_e = set()
        for rec in records or []:
            if not isinstance(rec, dict):
                continue
            low = {}
            for k, v in rec.items():
                lk = str(k).strip().lower().replace(" ", "").replace("_", "")
                if lk not in low and v not in (None, ""):
                    low[lk] = v
            if not low:
                continue
            # ── date: key me 'date'/'day' ho, ya value date jaisi lage ──
            date_raw = ""
            for lk, v in low.items():
                if "date" in lk or lk in ("day", "attdt", "attendancedt", "attdate"):
                    m = self._DAILY_DATE_RE.search(str(v))
                    date_raw = m.group(0) if m else str(v).strip()[:18]
                    if date_raw:
                        break
            if not date_raw:
                for v in low.values():
                    m = self._DAILY_DATE_RE.search(str(v))
                    if m:
                        date_raw = m.group(0)
                        break
            if not date_raw:
                continue
            # ── status: naam se pehle, phir value-shape se ──
            status = ""
            for lk, v in low.items():
                if "status" in lk or lk in (
                    "attendance", "att", "pa", "p/a", "ispresent",
                    "present", "absent", "ispresentabsent", "remark",
                    "presence", "attendancestatus",
                ):
                    status = _norm_status(v)
                    if status:
                        break
            if not status:
                for v in low.values():
                    if self._DAILY_STATUS_RE.match(str(v).strip()):
                        status = _norm_status(v)
                        if status:
                            break
            if not status:
                continue
            # ── time / code / title (optional garnish) ──
            time_txt = ""
            for lk, v in low.items():
                if any(w in lk for w in ("time", "slot", "period")) or lk in (
                    "lecture", "timing",
                ):
                    m = self._DAILY_TIME_RE.search(str(v))
                    if m:
                        time_txt = m.group(0)
                        break
            code = ""
            for lk, v in low.items():
                if lk.endswith("code") or lk in ("course", "subject"):
                    code_m = self._COURSE_CODE_RE.search(str(v))
                    code = code_m.group(1) if code_m else str(v).strip()[:16]
                    if code:
                        break
            title = ""
            for lk, v in low.items():
                if any(w in lk for w in ("title", "subject", "course", "name")):
                    s = str(v).strip()
                    if len(s) >= 6 and re.search(r"[A-Za-z]{3}", s):
                        if len(s) > len(title):
                            title = s
            key = (date_raw[:18], (time_txt or "")[:20], (code or "")[:16], status)
            if key in seen_e:
                continue
            seen_e.add(key)
            clean_title = self._COURSE_CODE_RE.sub("", title or "").strip(
                " :-–—|"
            )
            entries.append({
                "date": key[0],
                "time": key[1],
                "code": key[2],
                "title": (clean_title or title or code or "Lecture").strip()[:70],
                "status": status,
                "tone": self._daily_tone(status),
            })
        return entries

    def scrape_student_profile(self, cookies_dict=None):
        """⭐ Student Profile (frmStudentProfile.aspx): official student
        record - photo + personal/academic details. Best-effort, SIRF
        login ke waqt (profile roz nahi badalta, realtime sync me nahi).

        UIMS profile pages bhi wahi 2-column LABEL|VALUE detail pages
        hote hain - generic kv/sections parser (_parse_hostel_page)
        as-is reuse hota hai. Extra jo yahan nikalte hain:
          - photo_url: profile photo ka PORTAL URL (template me direct
            <img> nahi chalta - portal cookies browser me nahi hoti,
            isliye views.profile_photo_view se proxy hota hai)
          - name: header avatar/greeting ke liye student ka naam

        Debug ke liye HAMESHA profile_debug.txt dump hota hai - output
        galat/khali aaye to wahi file paste karo.
        """
        if cookies_dict:
            self.session.cookies = requests.utils.cookiejar_from_dict(cookies_dict)

        result = {
            "success": False,
            "found": False,
            "page_title": "",
            "source_url": "",
            "name": "",
            "photo_url": "",
            "photo_b64": "",   # ⭐ embedded base64 photo (data-URI) - FIX 4
            "photo_mime": "",  # ⭐ uska mime type (image/jpeg etc.)
            "kv": [],
            "sections": [],
        }
        debug_lines = []
        profile_url = self.auth_url + "frmStudentProfile.aspx"

        try:
            resp = self.session.get(profile_url, timeout=12, allow_redirects=True)
        except Exception as exc:
            debug_lines.append(f"GET {profile_url} -> error {exc}")
            self._dump_profile_debug(debug_lines)
            return result

        final_url = str(resp.url)
        debug_lines.append(
            f"GET {profile_url} -> {resp.status_code} "
            f"final={final_url} len={len(resp.text)}"
        )
        if "login" in final_url.lower():
            debug_lines.append("Login.aspx pe redirect - portal session dead tha")
            self._dump_profile_debug(debug_lines)
            return result

        soup = BeautifulSoup(resp.text, "html.parser")
        title_tag = soup.find("title")
        page_title = title_tag.get_text(" ", strip=True) if title_tag else ""
        debug_lines.append(f"TITLE {page_title}")

        # ── Generic detail-page extraction (hostel wala parser) ──
        kv, sections = self._parse_hostel_page(soup)

        # ⭐ FIX 1: UIMS profile ka personal info aksar 3-cell rows me hota
        # hai - LABEL | : | VALUE (colon apna alag cell). Purana parser sirf
        # 2-cell samajhta tha, isliye KV=0 aaya tha. Known profile-fields
        # ke saath dobara scan (2-cell + colon-3-cell dono).
        FIELD_RE = re.compile(
            r"(student name|father|mother|guardian|date of birth|d\.?o\.?b|"
            r"gender|blood|nationality|category|religion|mobile|phone|"
            r"e-?mail|address|city|district|state|pin|aadha?r|registration|"
            r"regd|roll|enroll|admission|batch|section|semester|course|"
            r"branch|program|uid|merit|scholar)",
            re.I,
        )
        seen_kv = {i["label"].lower() for i in kv}
        for tr in soup.find_all("tr"):
            cells = [
                re.sub(r"\s+", " ", c.get_text(" ", strip=True))
                for c in tr.find_all(["th", "td"], recursive=False)
            ]
            cells = [c for c in cells if c and c.strip()]
            label = value = ""
            if len(cells) == 3 and cells[1] in (":", "-", "="):
                label, value = cells[0], cells[2]   # ⭐ colon-cell wala row
            elif len(cells) == 2:
                label, value = cells[0], cells[1]
            else:
                continue
            label = label.rstrip(":").strip()
            value = value.strip()
            if not (1 < len(label) <= 42 and 0 < len(value) <= 90):
                continue
            if not FIELD_RE.search(label):
                continue
            key = label.lower()
            if key in seen_kv or value in (":", "-") or value.lower() == key:
                continue
            seen_kv.add(key)
            kv.append({"label": label[:60], "value": value[:90]})

        # Colon-wale rows ab kv card me hain - un tables ke SECTION duplicate
        # na dikhen, drop kar do (personal info sirf Details card me rahegi).
        def _is_colon_row(row):
            return len(row) == 3 and str(row[1]).strip() in (":", "-", "=")
        sections = [
            sec for sec in sections
            if not (sec["rows"] and sum(
                1 for r in sec["rows"] if _is_colon_row(r)
            ) >= max(1, len(sec["rows"]) // 2))
        ]

        # ⭐ FIX 5: "Contact Details" jaisi info-table se student ke APNE
        # contacts (Mobile/Email) Details card me kheencho. UIMS ye LABEL:
        # VALUE rows me NAHI deta - ek multi-column table deta hai
        # (Contact Type | Residence | Office | Mobile | EmailId) jisme
        # "Student"/"Father"/"Mother" owner-rows hoti hain. ZAROORI: RAW
        # table se padho aur EMPTY cells mat drop karo - warna column
        # index hil jaata hai (sections list me empties parse ke waqt hi
        # drop ho chuki hain, isliye sections se nahi, soup se padhte hain).
        contact_parent_re = re.compile(r"father|mother|parent|guardian", re.I)
        contact_kv_added = 0
        for table in soup.find_all("table"):
            if contact_kv_added >= 8:
                break
            raw_rows = []
            for tr in table.find_all("tr"):
                cells = [
                    re.sub(r"\s+", " ", c.get_text(" ", strip=True))
                    for c in tr.find_all(["th", "td"], recursive=False)
                ]
                if any(cells):
                    raw_rows.append(cells)
            if len(raw_rows) < 2:
                continue
            header_at = -1
            header_cols = {}
            for ri, cells in enumerate(raw_rows[:3]):
                joined = " ".join(cells).lower()
                if not any(w in joined for w in ("mobile", "email", "phone")):
                    continue
                for ci, cell in enumerate(cells):
                    low = cell.lower()
                    if "mobile" in low or "phone" in low:
                        header_cols.setdefault("mobile", ci)
                    if "email" in low:
                        header_cols.setdefault("email", ci)
                if header_cols:
                    header_at = ri
                    break
            if header_at < 0:
                continue
            for cells in raw_rows[header_at + 1:]:
                owner = cells[0].strip().rstrip(":") if cells else ""
                owner_low = owner.lower()
                if not owner or len(owner) > 30:
                    continue
                if re.match(r"^(student|self|candidate|applicant)$", owner_low):
                    prefix = ""
                elif contact_parent_re.search(owner_low):
                    prefix = owner.title().split()[0] + "'s "
                else:
                    continue
                for kind, label in (("mobile", "Mobile"), ("email", "Email")):
                    ci = header_cols.get(kind)
                    if ci is None or ci >= len(cells):
                        continue
                    value = cells[ci].strip(" :-")
                    if not value or value == "-":
                        continue
                    # column-shift guards: mobile me digits, email me @
                    if kind == "mobile" and len(re.sub(r"\D", "", value)) < 6:
                        continue
                    if kind == "email" and "@" not in value:
                        continue
                    full_label = (prefix + label).strip()
                    key = full_label.lower()
                    if key in seen_kv:
                        continue
                    seen_kv.add(key)
                    kv.append({"label": full_label[:60], "value": value[:90]})
                    contact_kv_added += 1
        if contact_kv_added:
            debug_lines.append(f"CONTACT-KV added={contact_kv_added}")

        # Owner-words (Student/Father/Mother) kabhi field-LABEL nahi hote -
        # empty cells ki wajah se column shift ho jaye to generic 2-col
        # scan unhe kisi value ke saath jod deta hai (junk "Student=email").
        kv = [item for item in kv if item["label"].strip().lower() not in (
            "student", "father", "mother", "guardian", "parent", "self")]

        debug_lines.append(f"KV {len(kv)}: " + "; ".join(
            f"{i['label']}={i['value']}" for i in kv[:20]
        ))
        debug_lines.append(f"SECTIONS {len(sections)}: " + "; ".join(
            f"{s['heading']}({len(s['rows'])})" for s in sections
        ))

        # ⭐ FIX 4: Photo pakadna - UIMS ki ASLI profile photo EMBEDDED
        # base64 data-URI hoti hai (<img id="imgFullProfilePic"
        # src="data:image;base64,/9j/...">). Purana scanner data-URI skip
        # karta tha, aur page ke top ke loader.gif (spinning "Loading"
        # animation - path me "upload" aa jaata hai) ko photo bana deta
        # tha. Ab teen niyam:
        #   1) id/class/alt jisme profile/student/avatar/photo ho = TOP
        #      priority (imgFullProfilePic aise hi pakda jaata hai)
        #   2) data:image...;base64 src = DIRECT capture - photo session
        #      me hi embed ho jaata hai, portal-fetch ki zaroorat nahi
        #   3) loader/spinner/logo/icon/hamburger src KABHI photo nahi
        photo_url = ""
        photo_b64 = ""
        photo_mime = ""
        photo_re = re.compile(
            r"(photo|picture|imagehandler|showimage|getimage|studentimage|"
            r"imgstu|student|\.ashx|upload)", re.I)
        key_ident_re = re.compile(
            r"(profile|student|user[-_ ]?pic|avatar|photo)", re.I)
        bad_img_re = re.compile(
            r"(loader|loading|spinner|preloader|logo|banner|icon|sprite|"
            r"button|arrow|favicon|hamburger|placeholder|no[-_ ]?image|"
            r"default[-_ ]?(img|image|photo|pic|user|avatar)|close)", re.I)
        # ⭐ "btn" sirf alag word/segment ho tab skip (btn_search.png),
        # warna ImgBtn jaisi legit photo ids bhi kat jaati hain.
        btn_re = re.compile(r"(^|[/_\-.])btn([/_\-.]|$)", re.I)

        def _img_parts(img):
            src = ((img.get("src") or img.get("data-src")
                    or img.get("data-original") or "")).strip()
            ident = " ".join([
                img.get("id") or "",
                " ".join(img.get("class") or []),
                img.get("alt") or "",
                img.get("name") or "",
            ]).strip()
            return src, ident

        def _src_head(src):
            # data-uri me sirf scheme/mime part pe bad-word check - PAYLOAD
            # pe nahi (base64 text me "icon"/"logo" jaisi random strings aa
            # sakti hain, asli photo galat skip ho jaati).
            low = src.lower()
            return low[:64] if low.startswith("data:") else low

        def _mime_sniff(b64_text, declared):
            declared_low = (declared or "").strip().lower()
            if "/" in declared_low:  # "image/jpeg" jaisa proper mime hi hai
                return declared_low
            # "data:image;base64" (subtype NULL hai) - magic bytes se guess
            if b64_text.startswith("/9j/"):
                return "image/jpeg"
            if b64_text.startswith("iVBOR"):
                return "image/png"
            if b64_text.startswith("R0lGOD"):
                return "image/gif"
            return "image/jpeg"

        def _capture_photo(src):
            nonlocal photo_url, photo_b64, photo_mime
            low = src.lower()
            if low.startswith("data:"):
                if not low.startswith("data:image"):
                    return False
                b64_match = re.match(
                    r"^data:([^;,]*);base64,(.+)$", src, re.I | re.S)
                if not b64_match:
                    return False
                candidate = re.sub(r"\s+", "", b64_match.group(2))
                if len(candidate) <= 200:  # choti fragment photo nahi hoti
                    return False
                photo_b64 = candidate
                photo_mime = _mime_sniff(candidate, b64_match.group(1))
                return True
            photo_url = urljoin(self.auth_url, src)
            return True

        img_like = list(soup.find_all("img"))
        for inp in soup.find_all("input"):
            if (inp.get("type") or "").lower() == "image":
                img_like.append(inp)

        # PASS 1: identity-keyword wali img (imgFullProfilePic / alt=Profile)
        for img in img_like:
            src, ident = _img_parts(img)
            if not src or bad_img_re.search(_src_head(src)) or bad_img_re.search(ident):
                continue
            if key_ident_re.search(ident) and _capture_photo(src):
                break

        # PASS 2 (fallback): photo-keyword wali SRC - purana behaviour,
        # lekin ab loader-skip ke saath. data-URI sirf PASS 1 me li jaati
        # hai (warna koi bhi anonymous embedded img photo ban jaati).
        if not (photo_url or photo_b64):
            for img in img_like:
                src, ident = _img_parts(img)
                if not src or src.lower().startswith("data:"):
                    continue
                blob = src + " " + ident
                if bad_img_re.search(blob) or btn_re.search(blob):
                    continue
                if photo_re.search(blob) and _capture_photo(src):
                    break

        debug_lines.append("PHOTO {0}".format(
            ("embedded-base64 mime=%s len=%d" % (photo_mime, len(photo_b64)))
            if photo_b64 else (photo_url or "-")))

        # ── Student ka naam (header avatar/display ke liye) ──
        name = ""
        for item in kv:
            lbl = item["label"].lower()
            if "name" not in lbl:
                continue
            if any(w in lbl for w in ("father", "mother", "parent", "guard",
                                      "mentor", "course", "program", "scheme",
                                      "school", "institute", "university")):
                continue
            name = item["value"].strip()
            break
        if not name:
            # asp:Label jaisa koi element ho jiske id me 'name' ho
            # (lblStudentName etc.) - uska TEXT hi naam hota hai.
            bad_id = ("father", "mother", "guard", "course", "college",
                      "fname", "mname", "program")
            for el in soup.find_all(id=re.compile(r"name", re.I)):
                el_id = (el.get("id") or "").lower()
                if any(w in el_id for w in bad_id):
                    continue
                txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
                if 2 < len(txt) <= 60 and ":" not in txt and "name" not in txt.lower():
                    name = txt
                    break
        # ALL-CAPS portal naam ko display-friendly banao (ADARSH SINGH -> Adarsh Singh)
        if name and name.upper() == name:
            name = name.title()
        debug_lines.append(f"NAME {name or '-'}")

        # ⭐ FIX 3: Debug deta hai, structure bhi - agli baar koi field miss
        # ho to isi dump se exact element pakda jayega (img/id/table map).
        for i, img in enumerate(soup.find_all("img")[:12]):
            debug_lines.append("IMG%d id=%s src=%s alt=%s" % (
                i, (img.get("id") or "-")[:40],
                (img.get("src") or "-")[:110], (img.get("alt") or "-")[:30]))
        id_count = 0
        for el in soup.find_all(id=True):
            if el.name in ("script", "style") or id_count >= 28:
                continue
            txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
            if not txt:
                continue
            debug_lines.append("ID <%s> %s = %s" % (
                el.name, (el.get("id") or "")[:46], txt[:60]))
            id_count += 1
        for ti, table in enumerate(soup.find_all("table")[:10]):
            trs = table.find_all("tr")
            sample = []
            for tr in trs[:3]:
                prow = [re.sub(r"\s+", " ", c.get_text(" ", strip=True))[:22]
                        for c in tr.find_all(["th", "td"], recursive=False)]
                prow = [c for c in prow if c]
                if prow:
                    sample.append(" | ".join(prow))
            debug_lines.append("TABLE%d rows=%d :: %s" % (
                ti, len(trs), " // ".join(sample)[:160]))

        blob = (page_title + " " + " ".join(i["label"] for i in kv[:8])).lower()
        found = (
            "profile" in blob or "personal" in blob or "student" in blob
            or len(kv) >= 2 or bool(sections)
        )
        result.update({
            "success": True,
            "found": bool(found),
            "page_title": page_title,
            "source_url": final_url,
            "name": name,
            "photo_url": photo_url,
            "photo_b64": photo_b64,
            "photo_mime": photo_mime,
            "kv": kv,
            "sections": sections,
        })
        debug_lines.append(
            f"RESULT found={result['found']} kv={len(kv)} "
            f"sections={len(sections)} contact-kv={contact_kv_added} "
            f"photo={'E' if photo_b64 else ('Y' if photo_url else 'N')}"
        )
        self._dump_profile_debug(debug_lines)

        print(
            f"[Profile] found={result['found']} kv={len(kv)} "
            f"sections={len(sections)} "
            f"photo={'E' if photo_b64 else ('Y' if photo_url else 'N')} "
            f"name={name or '-'}"
        )
        return result

    def _dump_profile_debug(self, debug_lines):
        """profile_debug.txt hamesha likho (parse-issue debug ka source)."""
        try:
            with open("profile_debug.txt", "w", encoding="utf-8") as debug_file:
                debug_file.write("\n".join(debug_lines))
        except OSError:
            pass

    def fetch_profile_photo(self, cookies_dict, photo_url):
        """Profile photo ke bytes lao (views.profile_photo_view se serve
        hota hai). Cookie-leak guard: photo SIRF apne portal host se
        fetch hoga - kisi aur host pe cookies kabhi nahi bhejte."""
        if cookies_dict:
            self.session.cookies = requests.utils.cookiejar_from_dict(cookies_dict)
        host = urlparse(self.base_url).netloc.lower()
        target_host = urlparse(photo_url or "").netloc.lower()
        if not photo_url or target_host != host:
            print(f"[Profile] photo REFUSED (host mismatch): {(photo_url or '-')[:80]}")
            return {"ok": False, "status": 0}
        try:
            resp = self.session.get(photo_url, timeout=12)
        except Exception as exc:
            print(f"[Profile] photo error: {exc}")
            return {"ok": False, "status": 0}
        if resp.status_code != 200 or "login" in str(resp.url).lower():
            return {"ok": False, "status": getattr(resp, "status_code", 0)}
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
        if not ctype.startswith("image/"):
            ctype = "image/jpeg"
        print(f"[Profile] photo served - {len(resp.content)} bytes")
        return {"ok": True, "content": resp.content, "content_type": ctype}

    def _parse_hostel_page(self, soup):
        """Hostel page se generic label:value (kv) aur tables nikaalo.

        UIMS detail pages aksar 2-column tables me dete hain (LABEL | VALUE)
        - unhe kv list me merge karte hain. Bade tables (3+ columns) ko
        section bana ke raw rows ke saath dete hain.
        """
        for tag in soup.find_all(["script", "style", "noscript", "link"]):
            tag.decompose()

        kv = []
        sections = []
        seen_kv, seen_sec = set(), set()
        label_re = re.compile(r"^[A-Za-z][A-Za-z /().&'+-]{1,45}:?$")
        skip_words = (
            "logout", "log out", "copyright", "all rights", "designed by",
            "sign out", "change password",
        )

        for table in soup.find_all("table"):
            rows = []
            for tr in table.find_all("tr"):
                cells = [
                    re.sub(r"\s+", " ", c.get_text(" ", strip=True))
                    for c in tr.find_all(["th", "td"], recursive=False)
                ]
                cells = [c[:90] for c in cells if c and c.strip()]
                if cells:
                    rows.append(cells)
            if not rows:
                continue

            # 2-column label:value table ho to kv me le jao.
            kv_hits = 0
            for row in rows:
                if len(row) != 2:
                    continue
                label, value = row[0].rstrip(":").strip(), row[1].strip()
                blob = (label + " " + value).lower()
                if any(w in blob for w in skip_words):
                    continue
                if not label_re.match(label) or value == label:
                    continue
                if len(value) > 90:
                    continue
                key = label.lower()
                if key not in seen_kv:
                    seen_kv.add(key)
                    kv.append({"label": label, "value": value})
                    kv_hits += 1

            if kv_hits >= max(1, len(rows) // 2):
                continue  # poora table kv me chala gaya

            # Warna section table - junk rows filter karke.
            clean = []
            for row in rows:
                joined = " ".join(row).lower()
                if any(w in joined for w in skip_words):
                    continue
                if len(row) == 1 and len(row[0]) < 3:
                    continue
                sig = "|".join(cell.lower() for cell in row)
                if sig in seen_sec:
                    continue
                seen_sec.add(sig)
                clean.append(row)
            if len(clean) < 2:
                continue

            heading_tag = table.find_previous(["h1", "h2", "h3", "h4", "h5"])
            heading = (
                re.sub(r"\s+", " ", heading_tag.get_text(" ", strip=True))[:60]
                if heading_tag else ""
            )
            sections.append({"heading": heading or "Details", "rows": clean[:15]})

        return kv, sections

    _COURSE_CODE_RE = re.compile(
        r"(?<![A-Z0-9])(\d{2}[A-Z]{2,4}[-\s]?\d{3,4}[A-Z]?)(?![A-Z0-9])",
        re.I,
    )
    # ⭐ Grid ke header-label cells - kabhi-kabhi header row pehle course
    # row ke saath MERGE ho jaata hai, to ye words title/meta me ghus
    # jaate hain ("Course Code Course Name Section Type Lecture Plan
    # Cloud Security..." jaisa junk). Exact-match pe skip.
    _COURSE_HEADER_CELLS = frozenset((
        "course code", "course name", "coursename", "course", "code",
        "name", "section", "type", "lecture plan", "plan", "remark",
        "sl no", "s.no", "sno", "sr no", "subject", "subject code",
        "subject name", "credits", "credit", "faculty",
    ))
    # ⭐ "Download PDF" / "View Plan" button TEXT hai, meta/title nahi.
    _PLAN_BUTTON_TEXT_RE = re.compile(
        r"^((download|view|show|open|get|click)\s+)*(pdf|(lecture\s+)?plan)$",
        re.I,
    )
    # ⭐ Header words kabhi-kabhi cell ke ANDAR chipke hote hain (nested
    # grid flatten ho ke) - cell text se peel kar do.
    _COURSE_HEADER_WORDS_RE = re.compile(
        r"\b(course\s+code|course\s+name|lecture\s+plan|"
        r"subject(?:\s+(?:code|name))?|section|remark|credits?|faculty|"
        r"s\.?\s*no|sr\.?\s*no|type)\b",
        re.I,
    )
    _COURSE_PLAN_WORDS = (
        "lecture", "lec no", "lec.", "topic", "unit", "coverage",
        "chapter", "content", "date", "plan",
    )

    def scrape_course_plan(self, cookies_dict=None):
        """⭐ My Courses + Lecture Plan (frmMyCourse.aspx) - best effort.

        Page ke TABLES se courses nikaalta hai (course-code pattern se:
        25CSH-210 jaisa). Har course ki lecture-plan tables (inline
        accordion ho to directly, ya plain .aspx link ho to follow
        karke - GET only, kabhi form-submit nahi) parse karta hai.

        Session safety: sirf GET requests. Login redirect mila to turant
        ruk jata hai (views me ye scrape health-probe ke PEHLE bulaya
        gaya hai, probe session restore kar dega).

        Debug: HAMESHA course_plan_debug.txt dump hota hai - output
        galat/khali aaye to wahi file paste karo.
        """
        if cookies_dict:
            self.session.cookies = requests.utils.cookiejar_from_dict(cookies_dict)

        result = {
            "success": False,
            "found": False,
            "source_url": "",
            "page_title": "",
            "courses": [],
            "extras": [],
            "page_pdfs": [],
        }
        debug = []
        url = self.auth_url + "frmMyCourse.aspx"

        try:
            resp = self.session.get(url, timeout=12, allow_redirects=True)
        except Exception as exc:
            debug.append(f"GET {url} -> error {exc}")
            self._dump_course_plan_debug(debug)
            print("[Courses] found=False (network) - course_plan_debug.txt")
            return result

        debug.append(f"GET {url} -> {resp.status_code} final={resp.url} len={len(resp.text)}")

        if "login" in resp.url.lower() or "txtloginpassword" in resp.text.lower():
            debug.append("LOGIN-REDIRECT detected; stopped.")
            self._dump_course_plan_debug(debug)
            print("[Courses] found=False (login redirect)")
            return result

        soup = BeautifulSoup(resp.text, "html.parser")
        page_title = ""
        if soup.title and soup.title.string:
            page_title = soup.title.string.strip()

        courses, extras, page_pdfs = self._parse_course_plan_page(soup, url)
        debug.append(
            f"PARSED courses={len(courses)} extras={len(extras)} "
            f"page_pdfs={len(page_pdfs)}"
        )
        for c in courses:
            debug.append(
                f"COURSE {c['code']} | title={c['title'][:40]!r} meta={c['meta']} "
                f"pdf={c.get('plan_pdf') or '-'} "
                f"plan_url={c.get('plan_url') or '-'} "
                f"postback={c.get('postback') or '-'} "
                f"button={c.get('plan_button') or '-'}"
            )
        for item in page_pdfs:
            debug.append(f"PAGE-PDF {item['label'][:40]!r} -> {item['url']}")

        # ⭐ Lecture plan asli me PDF format me hota hai (user-confirmed)
        # - isliye HTML tables dhoondhne wala v1/v2 login-flow plans=0 de
        # raha tha. Ab login pe har course ke peeche requests/PDFs
        # NAHI kholte (login bahut slow ho gaya tha ~20s): sirf REFS
        # (pdf href / aspx href / __doPostBack target) save karke chhodte
        # hain. Asli plan user ke CLICK pe laata hai
        # fetch_course_plan_document() <- views.course_plan_pdf_view
        # (fee-receipt proxy wala hi safe pattern - WYSIWYG official PDF).

        # Debug ke liye page ka visible text sample bhi dump karo
        try:
            text_sample = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:1200]
            debug.append("TEXT SAMPLE: " + text_sample)
        except Exception:
            pass

        result["courses"] = courses[:16]
        result["extras"] = extras[:4]
        result["page_pdfs"] = page_pdfs[:6]
        result["found"] = bool(courses or extras or page_pdfs)
        result["success"] = result["found"]
        result["page_title"] = page_title
        result["source_url"] = resp.url

        plan_links = sum(
            1 for c in result["courses"]
            if c["plan"] or c.get("plan_pdf") or c.get("plan_url")
            or c.get("postback") or c.get("plan_button")
        )
        debug.append(
            f"RESULT found={result['found']} courses={len(result['courses'])} "
            f"plan-links={plan_links} page-pdfs={len(result['page_pdfs'])}"
        )
        self._dump_course_plan_debug(debug)
        print(
            f"[Courses] found={result['found']} courses={len(result['courses'])} "
            f"plan-links={plan_links} page-pdfs={len(result['page_pdfs'])}"
        )
        return result

    def _dump_course_plan_debug(self, lines):
        try:
            with open("course_plan_debug.txt", "w", encoding="utf-8") as dbg:
                dbg.write("\n".join(lines))
        except OSError:
            pass

    def _extract_hidden_fields(self, soup):
        """ASP.NET form ke hidden inputs (__VIEWSTATE, __EVENTVALIDATION
        etc.) - postback ke liye. VIEWSTATE nahi mila to {} (post skip)."""
        fields = {}
        form = soup.find("form")
        scope = form if form is not None else soup
        for inp in scope.find_all("input"):
            if (inp.get("type") or "").lower() != "hidden":
                continue
            name = inp.get("name")
            if name:
                fields[name] = inp.get("value", "")
        return fields if "__VIEWSTATE" in fields else {}

    def _norm_course_code(self, code):
        return re.sub(r"[\s-]+", "", str(code).upper())

    def _parse_course_plan_page(self, soup, base_url):
        """frmMyCourse.aspx se courses + inline lecture-plan tables.

        Course-row = jisme course-code pattern mile (25CSH-210). Sabse
        lamba text-cell = title, baaki chhote cells = meta chips. Table
        me koi course-code NA ho par header lecture/topic/unit jaisa ho
        to wo plan-table hai - last seen course ka plan (ya extras).
        """
        for tag in soup.find_all(["script", "style", "noscript", "link"]):
            tag.decompose()

        skip_words = (
            "logout", "log out", "copyright", "all rights",
            "designed by", "sign out", "change password",
        )
        courses = []
        extras = []
        seen_codes = set()
        last_course = None

        for table in soup.find_all("table"):
            rows = []
            for tr in table.find_all("tr"):
                cells = [
                    re.sub(r"\s+", " ", c.get_text(" ", strip=True))
                    for c in tr.find_all(["th", "td"], recursive=False)
                ]
                cells = [c[:120] for c in cells if c and c.strip()]
                links = []
                for anchor in tr.find_all("a", href=True):
                    href = anchor["href"].strip()
                    low = href.lower()
                    if low.startswith("javascript"):
                        links.append(("js", href))
                    elif not low or href.startswith("#"):
                        continue
                    elif ".pdf" in low:
                        # ⭐ Lecture plan asli me PDF hota hai
                        links.append(("pdf", urljoin(base_url, href)))
                    elif ".aspx" in low or "plan" in low or "download" in low:
                        links.append(("get", urljoin(base_url, href)))
                # ⭐ Portal ka "Download PDF" anchor nahi, ASP.NET
                # ButtonField (<input type=submit/image>) ya onclick=
                # __doPostBack wala element bhi ho sakta hai - dono
                # capture karo warna plan-link MISS ho jaata hai.
                for tagged in tr.find_all(
                    attrs={"onclick": re.compile(r"__doPostBack", re.I)}
                ):
                    onclick_match = re.search(
                        r"__doPostBack\('([^']*)'\s*,\s*'([^']*)'\)",
                        tagged.get("onclick", ""),
                    )
                    if onclick_match:
                        links.append((
                            "js",
                            "javascript:__doPostBack('%s','%s')"
                            % onclick_match.groups(),
                        ))
                for inp in tr.find_all("input"):
                    itype = (inp.get("type") or "").lower()
                    if itype not in ("submit", "image", "button"):
                        continue
                    iname = inp.get("name") or ""
                    if not iname:
                        continue
                    ivalue = inp.get("value", "") or ""
                    blob = " ".join([iname, ivalue, inp.get("src") or ""]).lower()
                    if any(w in blob for w in ("plan", "pdf", "download")):
                        links.append(("button", [iname, ivalue, itype]))
                if cells:
                    # ⭐ raw_blob UNTRUNCATED rakho: cells[:120] kaatne se
                    # wrapper mega-cell ke baaki course-codes gaayab ho
                    # jaate hain aur wrapper-skip guard phail ho jaata
                    # hai (pehla course ka title junk ban tha).
                    raw_blob = re.sub(
                        r"\s+", " ", tr.get_text(" ", strip=True)
                    )[:2000]
                    rows.append((cells, links, raw_blob))
            if not rows:
                continue

            code_rows = []
            for idx, (cells, links, raw_blob) in enumerate(rows):
                match = self._COURSE_CODE_RE.search(raw_blob)
                if not match:
                    continue
                # ⭐ Outer WRAPPER row jo poore nested grid ko ek hi cell
                # me flatten kar deti hai (row ke RAW text me 2+ alag
                # course codes) - skip karo; andar ke asli per-course
                # rows page ke doosre tables se saaf mil jaati hain.
                # Warna pehle course ka title "Course Code Course Name
                # Section..." jaisa junk ban jaata hai.
                distinct_codes = {
                    self._norm_course_code(m.group(1))
                    for m in self._COURSE_CODE_RE.finditer(raw_blob)
                }
                if len(distinct_codes) >= 2:
                    continue
                code_rows.append((idx, match.group(1), cells, links))

            if code_rows:
                # Header row: code-rows se pehle ka non-code header-y row
                header_cells = None
                first_code_idx = code_rows[0][0]
                if first_code_idx > 0:
                    head_blob = " ".join(rows[first_code_idx - 1][0]).lower()
                    if any(w in head_blob for w in ("course", "code", "subject", "faculty", "credit", "type", "plan")):
                        header_cells = rows[first_code_idx - 1][0]

                for idx, raw_code, cells, links in code_rows:
                    norm = self._norm_course_code(raw_code)
                    if norm in seen_codes:
                        continue
                    code = re.sub(r"\s+", " ", raw_code).upper().replace(" ", "-") \
                        if " " in raw_code and "-" not in raw_code else raw_code
                    title = ""
                    meta = []
                    for cell in cells:
                        cell_key = cell.strip().lower()
                        # ⭐ Header-junk (merged header row) skip karo -
                        # warna pehle course ka title "Course Code Course
                        # Name Section Type..." ban jaata hai.
                        if cell_key in self._COURSE_HEADER_CELLS:
                            continue
                        # ⭐ "Download PDF"/"View Plan" = button TEXT,
                        # meta chip / title nahi.
                        if self._PLAN_BUTTON_TEXT_RE.match(cell_key):
                            continue
                        # ⭐ Header words cell ke ANDAR bhi ho sakte hain
                        # (nested grid flatten) - peel karke aage badho.
                        clean = re.sub(
                            r"\s+",
                            " ",
                            self._COURSE_HEADER_WORDS_RE.sub(" ", cell),
                        ).strip(" :-–—|")
                        if not clean or self._PLAN_BUTTON_TEXT_RE.match(clean.lower()):
                            continue
                        if self._COURSE_CODE_RE.search(clean):
                            rest = self._COURSE_CODE_RE.sub("", clean).strip(" :-–—|")
                            if rest and not title and len(rest) >= 4:
                                title = rest
                            continue
                        blob = clean.lower()
                        if any(w in blob for w in skip_words):
                            continue
                        if len(clean) >= 8 and re.search(r"[A-Za-z]{3}", clean) and not title:
                            title = clean
                        elif clean and len(clean) <= 42 and clean not in meta:
                            meta.append(clean)
                    plan_url = next((u for kind, u in links if kind == "get"), "")
                    if plan_url.split("?")[0].lower() == base_url.lower():
                        plan_url = ""
                    # ⭐ Lecture plan PDF (direct .pdf href) - portal pe
                    # plan PDF format me hota hai.
                    plan_pdf = next((u for kind, u in links if kind == "pdf"), "")
                    # ⭐ "Download PDF" ASP.NET ButtonField (input submit/
                    # image) - click pe form-post se PDF aati hai.
                    plan_button = next(
                        (u for kind, u in links if kind == "button"), None
                    )
                    # ⭐ ASP.NET grid-select: __doPostBack('target','arg')
                    # capture karo - lecture plan ek safe form-post ke
                    # peeche bhi ho sakta hai (click pe proxy fetch hoga).
                    postback = None
                    for kind, raw in links:
                        if kind != "js":
                            continue
                        post_match = re.search(
                            r"__doPostBack\('([^']*)'\s*,\s*'([^']*)'\)",
                            raw,
                        )
                        if post_match:
                            postback = [post_match.group(1), post_match.group(2)]
                            break
                    seen_codes.add(norm)
                    course = {
                        "code": code,
                        "title": (title or code)[:110],
                        "meta": meta[:4],
                        "header": header_cells or [],
                        "plan": [],
                        "plan_pdf": plan_pdf,
                        "plan_url": plan_url,
                        "postback": postback,
                        "plan_button": plan_button,
                    }
                    courses.append(course)
                    last_course = course
                continue

            # Course-code nahi mila -> plan-table candidate?
            blob_all = " ".join(" ".join(cells) for cells, _, _rb in rows).lower()
            if any(w in blob_all for w in skip_words):
                continue
            header_hit = sum(
                1 for w in self._COURSE_PLAN_WORDS if w in " ".join(rows[0][0]).lower()
            )
            if header_hit == 0 and not any(
                w in blob_all for w in ("lecture", "topic", "unit")
            ):
                continue
            clean_rows = []
            for cells, _, _rb in rows:
                joined = " ".join(cells).lower()
                if any(w in joined for w in skip_words):
                    continue
                clean_rows.append(cells[:6])
            if len(clean_rows) < 2:
                continue
            section = {"heading": " ", "rows": clean_rows[:20]}
            # ⭐ inline accordion ho to last course ka plan hi hai ye
            if last_course is not None and not last_course["plan"]:
                last_course["plan"].append(section)
            else:
                extras.append(section)

        # Fallback: div/text layout (koi course table nahi mila)
        if not courses:
            for node in soup.find_all(string=self._COURSE_CODE_RE):
                match = self._COURSE_CODE_RE.search(node)
                norm = self._norm_course_code(match.group(1))
                if norm in seen_codes:
                    continue
                parent = node.parent
                block = re.sub(r"\s+", " ", parent.get_text(" ", strip=True))[:160]
                title = self._COURSE_CODE_RE.sub("", block).strip(" :-–—|")
                seen_codes.add(norm)
                courses.append({
                    "code": match.group(1),
                    "title": (title or match.group(1))[:110],
                    "meta": [],
                    "header": [],
                    "plan": [],
                    "plan_pdf": "",
                    "plan_url": "",
                    "postback": None,
                    "plan_button": None,
                })
                if len(courses) >= 16:
                    break

        # ⭐ Page-level lecture-plan links: table rows ke BAHAR bhi ho
        # sakte hain (top ka "Download Lecture Plan" button / menu card).
        # Jis link ke saath course-code match ho jaye us course ka
        # plan_pdf bana do; baaki page_pdfs list me rakho (panel ke top
        # pe alag se dikhenge). Sirf portal-host links (cookie-leak guard).
        base_host = urlparse(base_url).netloc.lower()
        page_pdfs = []
        # Rows me jo PDFs kisi course ko mil chukey hain unhe page-level
        # list me dubara mat lao (seed with row-attached urls).
        seen_pdf_urls = {
            c["plan_pdf"].lower() for c in courses if c.get("plan_pdf")
        }
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            low = href.lower()
            if low.startswith("javascript") or href.startswith("#"):
                continue
            if ".pdf" not in low and "plan" not in low:
                continue
            abs_url = urljoin(base_url, href)
            if urlparse(abs_url).netloc.lower() != base_host:
                continue
            key_url = abs_url.lower()
            if key_url in seen_pdf_urls:
                continue
            seen_pdf_urls.add(key_url)
            blob = anchor.get_text(" ", strip=True) + " " + href
            attached = False
            code_match = self._COURSE_CODE_RE.search(blob)
            if code_match:
                norm = self._norm_course_code(code_match.group(1))
                for course in courses:
                    if (
                        self._norm_course_code(course["code"]) == norm
                        and not course.get("plan_pdf")
                    ):
                        course["plan_pdf"] = abs_url
                        attached = True
                        break
            if not attached and ".pdf" in low:
                label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))[:60]
                page_pdfs.append({
                    "label": label or "Lecture Plan PDF",
                    "url": abs_url,
                })

        return courses, extras, page_pdfs

    def _extract_plan_tables(self, soup):
        """Lecture-plan page se plan tables (lecture/topic/unit rows)."""
        for tag in soup.find_all(["script", "style", "noscript", "link"]):
            tag.decompose()
        sections = []
        skip_words = (
            "logout", "log out", "copyright", "all rights",
            "designed by", "sign out", "change password",
        )
        for table in soup.find_all("table"):
            rows = []
            for tr in table.find_all("tr"):
                cells = [
                    re.sub(r"\s+", " ", c.get_text(" ", strip=True))
                    for c in tr.find_all(["th", "td"], recursive=False)
                ]
                cells = [c[:120] for c in cells if c and c.strip()]
                if cells:
                    rows.append(cells)
            if len(rows) < 2:
                continue
            blob_all = " ".join(" ".join(r) for r in rows).lower()
            if any(w in blob_all for w in skip_words):
                continue
            # ⭐ Vapas course-LIST grid ko plan-table mat samjho (uske
            # header me "Plan" word hota hai) - 2+ alag course-codes ka
            # table plan nahi ho sakta.
            codes_in_blob = {
                self._norm_course_code(m.group(1))
                for m in self._COURSE_CODE_RE.finditer(blob_all)
            }
            if len(codes_in_blob) >= 2:
                continue
            head_blob = " ".join(rows[0]).lower()
            if not any(w in head_blob for w in self._COURSE_PLAN_WORDS) and not any(
                w in blob_all for w in ("lecture", "topic", "unit")
            ):
                continue
            sections.append({"heading": "Lecture Plan", "rows": [r[:6] for r in rows[:20]]})
        return sections

    def fetch_course_plan_document(
        self,
        cookies_dict=None,
        page_url=None,
        plan_pdf=None,
        plan_url=None,
        postback=None,
        plan_button=None,
    ):
        """⭐ My Courses ka lecture plan ON-DEMAND lao (official PDF).

        Lecture plan portal pe PDF format me aata hai - isliye login pe
        kuch download nahi karte (scrape_course_plan sirf REFS rakhta
        hai). User ke CLICK pe ye method plan laata hai:
          plan_pdf    -> direct GET (official PDF bytes)
          plan_url    -> GET; PDF mile to PDF, warna HTML plan-table parse
          postback    -> FRESH __VIEWSTATE + __EVENTTARGET POST (grid-select)
          plan_button -> ASP.NET ButtonField ("Download PDF" input) ka
                         form-post: submit = name=value, image = name.x/.y
        Returns:
          {"kind": "pdf", "content": bytes}
          {"kind": "html", "sections": [...]}
          None  (link dead / portal session expired / plan nahi mila)
        Bytes kabhi parse nahi karte - WYSIWYG, wahi official plan milta
        hai jo portal pe dikhta hai.
        """
        if cookies_dict:
            self.session.cookies = requests.utils.cookiejar_from_dict(cookies_dict)

        page_url = urljoin(self.auth_url, page_url or "frmMyCourse.aspx")
        base_host = urlparse(page_url).netloc.lower()

        def classify(response):
            if "login" in response.url.lower():
                return None
            content = response.content or b""
            ctype = (response.headers.get("Content-Type") or "").lower()
            if content[:5] == b"%PDF-" or "pdf" in ctype:
                return {"kind": "pdf", "content": content}
            soup = BeautifulSoup(response.text, "html.parser")
            sections = self._extract_plan_tables(soup)
            if sections:
                return {"kind": "html", "sections": sections[:6]}
            return None

        # 1) Direct href pehle try karo (PDF ho to seedha mil jaata hai).
        for target in (plan_pdf, plan_url):
            if not target:
                continue
            target = str(target)
            if target.lower().startswith("javascript:") or target.startswith("#"):
                continue
            target = urljoin(page_url, target)
            if urlparse(target).netloc.lower() != base_host:
                continue  # cookie-leak guard: sirf apne portal se fetch
            try:
                resp = self.session.get(target, timeout=20, allow_redirects=True)
            except requests.RequestException:
                continue
            if resp.status_code != 200:
                continue
            document = classify(resp)
            if document:
                return document

        # 2) ASP.NET postback (grid-select) -> PDF download / plan page.
        #    ViewState har request pe badalta hai, isliye page FRESH GET.
        if postback and len(postback) == 2:
            try:
                base = self.session.get(page_url, timeout=15)
            except requests.RequestException:
                return None
            if "login" in base.url.lower():
                return None
            hidden = self._extract_hidden_fields(
                BeautifulSoup(base.text, "html.parser")
            )
            if not hidden:
                return None
            payload = dict(hidden)
            payload["__EVENTTARGET"] = str(postback[0])
            payload["__EVENTARGUMENT"] = str(postback[1])
            try:
                post_resp = self.session.post(
                    page_url,
                    data=payload,
                    timeout=20,
                    allow_redirects=True,
                )
            except requests.RequestException:
                return None
            if post_resp.status_code != 200:
                return None
            return classify(post_resp)

        # 3) ASP.NET ButtonField (<input type=submit/image "Download PDF">)
        #    - submit button ka form-post: name=value (image: name.x/.y).
        if plan_button and len(plan_button) >= 1:
            button_name = str(plan_button[0])
            button_value = str(plan_button[1]) if len(plan_button) > 1 else ""
            button_type = (
                str(plan_button[2]).lower() if len(plan_button) > 2 else "submit"
            )
            try:
                base = self.session.get(page_url, timeout=15)
            except requests.RequestException:
                return None
            if "login" in base.url.lower():
                return None
            hidden = self._extract_hidden_fields(
                BeautifulSoup(base.text, "html.parser")
            )
            if not hidden:
                return None
            payload = dict(hidden)
            if button_type == "image":
                payload[button_name + ".x"] = "1"
                payload[button_name + ".y"] = "1"
            else:
                payload[button_name] = button_value or "Download PDF"
            try:
                btn_resp = self.session.post(
                    page_url,
                    data=payload,
                    timeout=20,
                    allow_redirects=True,
                )
            except requests.RequestException:
                return None
            if btn_resp.status_code != 200:
                return None
            return classify(btn_resp)

        return None

    def scrape_extra_sections(self, cookies_dict=None):
        """Fetch notices, fees, datesheet and messages after login."""
        sections = {
            "notices": [
                "frmNoticeBoard.aspx",
                "frmNotice.aspx",
                "NoticeBoard.aspx",
                "frmStudentNotice.aspx",
            ],
            "fees": [
                "frmFeeDetails.aspx",
                "frmStudentFee.aspx",
                "frmFeePayment.aspx",
                "FeeDetails.aspx",
            ],
            "datesheet": [
                "frmDateSheet.aspx",
                "frmExamDateSheet.aspx",
                "DateSheet.aspx",
                "frmExamSchedule.aspx",
            ],
            "messages": [
                "frmMessages.aspx",
                "frmStudentMessages.aspx",
                "Messages.aspx",
                "frmAnnouncement.aspx",
            ],
        }

        # First discover actual menu links from the authenticated portal.
        # UIMS installations often use different .aspx filenames.
        keywords = {
            "notices": ("notice", "circular", "announcement"),
            "fees": ("fee", "payment", "dues", "finance"),
            "datesheet": ("date sheet", "datesheet", "exam schedule", "exam"),
            "messages": ("message", "communication", "inbox", "announcement"),
        }

        discovered = {name: [] for name in sections}
        for landing_path in (
            "",
            "default.aspx",
            "home.aspx",
            "frmStudentHome.aspx",
            "frmMyTimeTable.aspx",
            "frmStudentMarksView.aspx",
            "result.aspx",
            "frmStudentCourseWiseAttendanceSummary.aspx",
        ):
            try:
                landing_url = urljoin(self.auth_url, landing_path)
                landing_response = self.session.get(landing_url, timeout=10)
                if landing_response.status_code != 200 or "login" in landing_response.url.lower():
                    continue
                landing_soup = BeautifulSoup(landing_response.text, "html.parser")
                for anchor in landing_soup.find_all("a", href=True):
                    label = anchor.get_text(" ", strip=True).lower()
                    href = anchor.get("href", "").strip()
                    if not href or href.startswith("#") or href.lower().startswith("javascript:"):
                        continue
                    for name, words in keywords.items():
                        if any(word in label or word in href.lower() for word in words):
                            discovered[name].append(urljoin(landing_url, href))
            except requests.RequestException:
                continue

        try:
            with open("portal_links_debug.txt", "w", encoding="utf-8") as debug_file:
                for name, links in discovered.items():
                    debug_file.write(f"[{name}]\n")
                    for link in links:
                        debug_file.write(f"{link}\n")
        except OSError:
            pass

        result = {}
        for name, paths in sections.items():
            discovered_paths = discovered.get(name, [])
            result[name] = self.scrape_section_page(
                page_name=name,
                candidate_paths=discovered_paths + paths,
                cookies_dict=cookies_dict,
            )
            print(
                f"[Portal Section] {name}: "
                f"success={result[name].get('success')}, "
                f"url={result[name].get('url')}, "
                f"tables={len(result[name].get('tables', []))}, "
                f"cards={len(result[name].get('cards', []))}, "
                f"error={result[name].get('error')}"
            )
        return result

    def scrape_attendance_records(self, cookies_dict=None):
        if cookies_dict:
            self.session.cookies = requests.utils.cookiejar_from_dict(cookies_dict)

        endpoint = self.auth_url + "frmStudentCourseWiseAttendanceSummary.aspx"
        static_token = "?type=etgkYfqBdH1fSfc255iYGw=="

        try:
            response = self.session.get(endpoint + static_token, timeout=10)
            response.raise_for_status()
        except Exception as exc:
            return {"success": False, "error": f"Failed to access attendance logs: {exc}"}

        page = response.text
        if "Whoops, Something broke!" in page:
            return {"success": False, "error": "UIMS is under maintenance."}

        session_match = re.search(
            r"CurrentSession\s*\(\s*['\"]?([^'\")]+?)['\"]?\s*\)",
            page,
        )
        report_match = re.search(
            r"getReport\s*\(\s*['\"]([^'\"]+?)['\"]",
            page,
        )

        if not session_match or not report_match:
            return {"success": False, "error": "Attendance security keys could not be parsed."}

        service_url = endpoint + "/GetReport"
        headers = {
            "Content-Type": "application/json",
            "Referer": endpoint + static_token,
        }
        post_data = (
            "{UID:'%s',Session:'%s'}"
            % (report_match.group(1), session_match.group(1))
        )

        try:
            result = self.session.post(
                service_url,
                headers=headers,
                data=post_data,
                timeout=10,
            )
            result.raise_for_status()
            raw_data = result.json()["d"]
            # ⭐ v5.8 PORTAL OVERALL: portal ka APNA overall % nikalo (agar
            # records/page me hai). App ka weighted average portal ke
            # overall se alag ho sakta hai - portal wala authoritative.
            raw_records = json.loads(raw_data)
            overall, raw_records = self._portal_overall(page, raw_records)
            return {
                "success": True,
                "records": raw_records,
                "overall": overall,
            }
        except Exception as exc:
            return {"success": False, "error": f"Attendance parsing failed: {exc}"}

    @staticmethod
    def _portal_overall(page, records):
        """⭐ Portal-reported overall attendance % dhoondo.

        1) records me koi "overall"/"aggregate" pseudo-row ho (kuch
           portals footer-total bhejte hain) -> uska percentage lo aur
           wo row subject list se HATA do (na-to course card banta).
        2) summary page HTML me "Overall Attendance xx%" jaisa text.
        Na mile to None -> views weighted-average fallback karega.
        """
        overall = None
        keep = []
        for record in records or []:
            label = " ".join(
                str(record.get(key) or "")
                for key in ("Code", "Title", "Course", "CourseName", "Subject")
            ).lower()
            is_overall_row = ("overall" in label) or ("aggregate" in label)
            if is_overall_row:
                for key in (
                    "TotalPercentage", "Percentage", "TotalPerc",
                    "AttendancePercentage",
                ):
                    raw_pct = record.get(key)
                    if raw_pct is None:
                        continue
                    try:
                        overall = float(str(raw_pct).replace("%", "").strip())
                        break
                    except (TypeError, ValueError):
                        continue
                continue
            keep.append(record)
        if overall is None and page:
            # ⭐ Tags strip karke plain text pe match - "Overall...xx%" ya
            # "xx% ...Overall" dono layouts, alag-alag tags ho to bhi chale.
            text = re.sub(r"<[^>]+>", " ", page)
            text = re.sub(r"\s+", " ", text)
            match = re.search(
                r"(?:overall|aggregate)[^%]{0,60}?(\d{1,3}(?:\.\d{1,2})?)\s*%",
                text,
                re.I,
            )
            if not match:
                match = re.search(
                    r"(\d{1,3}(?:\.\d{1,2})?)\s*%[^%]{0,60}?(?:overall|aggregate)",
                    text,
                    re.I,
                )
            if match:
                try:
                    overall = float(match.group(1))
                except ValueError:
                    overall = None
        if overall is not None and not (0.0 <= overall <= 100.0):
            overall = None
        return overall, keep

    def scrape_timetable(self, cookies_dict=None):
        if cookies_dict:
            self.session.cookies = requests.utils.cookiejar_from_dict(cookies_dict)

        timetable_url = self.auth_url + "frmMyTimeTable.aspx"

        try:
            response = self.session.get(timetable_url, timeout=10)
            response.raise_for_status()
        except Exception as exc:
            return {"success": False, "error": f"Failed to access timetable: {exc}"}

        soup = BeautifulSoup(response.text, "html.parser")
        timetable_table = soup.find(
            "table",
            id=lambda value: value and ("gvMyTimeTable" in value or "grdMain" in value),
        )
        mapping_table = soup.find(
            "table",
            id=lambda value: value and (
                "gvMyTimeTableDetails" in value or "grdCourseDetail" in value
            ),
        )

        if timetable_table and mapping_table:
            return self._parse_timetable_html(
                soup,
                timetable_table,
                mapping_table,
            )

        data = self._hidden_fields(soup)
        data["__EVENTTARGET"] = (
            "ctl00$ContentPlaceHolder1$ReportViewer1$"
            "ctl09$Reserved_AsyncLoadTarget"
        )

        try:
            post_response = self.session.post(
                timetable_url,
                data=data,
                cookies=self.session.cookies,
                timeout=10,
            )
            post_response.raise_for_status()
            post_soup = BeautifulSoup(post_response.text, "html.parser")
            timetable_table = post_soup.find(
                "table",
                id=lambda value: value and ("gvMyTimeTable" in value or "grdMain" in value),
            )
            mapping_table = post_soup.find(
                "table",
                id=lambda value: value and (
                    "gvMyTimeTableDetails" in value or "grdCourseDetail" in value
                ),
            )

            if not timetable_table or not mapping_table:
                return {"success": False, "error": "Weekly timetable was not populated."}

            return self._parse_timetable_html(
                post_soup,
                timetable_table,
                mapping_table,
            )
        except Exception as exc:
            return {"success": False, "error": f"Timetable parsing failed: {exc}"}

    def _parse_timetable_html(self, soup, timetable_table, course_mapping_table):
        try:
            course_codes = {}
            for row in course_mapping_table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) > 1:
                    course_codes[cells[0].get_text(strip=True)] = cells[1].get_text(strip=True)

            rows = timetable_table.find_all("tr")
            if not rows:
                return {"success": True, "timetable": {}}

            headers = rows[0].find_all(["th", "td"])
            days = [cell.get_text(strip=True) for cell in headers[1:]]
            timetable = {day: [] for day in days}

            for row in rows[1:]:
                cells = row.find_all("td")
                if len(cells) <= 1:
                    continue

                time_value = cells[0].get_text(" ", strip=True)
                for day_index, cell in enumerate(cells[1:]):
                    raw_subject = cell.get_text(" ", strip=True)
                    if not raw_subject or day_index >= len(days):
                        continue
                    parsed = self._parse_subject_string(raw_subject, course_codes)
                    parsed["time"] = time_value
                    timetable[days[day_index]].append(parsed)

            return {"success": True, "timetable": timetable}
        except Exception as exc:
            return {"success": False, "error": f"Timetable parse error: {exc}"}

    # ⭐ Schedule colour palette - har subject course-code ke HASH se ek
    # fixed colour pakadta hai (Google Calendar jaisa: same subject hamesha
    # same colour). Hex direct template me CSS variable (--slot) banti hai
    # isliye Tailwind class-generation ki tension nahi.
    _SUBJECT_COLORS = (
        "#22D3EE",  # cyan
        "#FBBF24",  # amber
        "#A78BFA",  # violet
        "#34D399",  # emerald
        "#FB923C",  # orange
        "#F472B6",  # pink
        "#60A5FA",  # blue
        "#E879F9",  # fuchsia
    )

    @classmethod
    def _subject_color(cls, key):
        """Same subject-code -> hamesha same palette colour."""
        total = 0
        for char in str(key or "GENERIC"):
            total = (total * 31 + ord(char)) & 0xFFFFFFFF
        return cls._SUBJECT_COLORS[total % len(cls._SUBJECT_COLORS)]

    def _parse_subject_string(self, subject_str, course_codes):
        try:
            parts = subject_str.split("::", 1)
            code_and_type = parts[0].split(":", 1)
            code = code_and_type[0].strip()
            session_type = code_and_type[1].strip().upper() if len(code_and_type) > 1 else "L"

            title = course_codes.get(code, code).upper()
            details = parts[1].split(":", 1)
            group = details[0].replace("GP-", "").strip()

            teacher = "Prof. Staff"
            room = "N/A"
            if len(details) > 1:
                professor_info = details[1].split("at", 1)
                teacher = professor_info[0].replace("By", "").strip()
                if len(professor_info) > 1:
                    room = professor_info[1].strip()

            return {
                "code": code,
                "title": title,
                "type": "Lecture" if session_type == "L" else "Practical",
                "group": group,
                "teacher": teacher,
                "room": room,
                # ⭐ har subject ka apna fixed colour (schedule card strip/dot/time)
                "color": self._subject_color(code or title),
            }
        except Exception:
            return {
                "code": "GENERIC",
                "title": subject_str,
                "type": "Lecture",
                "group": "All",
                "teacher": "Prof. Staff",
                "room": "N/A",
                "color": self._subject_color(subject_str),
            }

    def scrape_marks_records(self, cookies_dict=None, session_id=None):
        if cookies_dict:
            self.session.cookies = requests.utils.cookiejar_from_dict(cookies_dict)

        marks_url = self.auth_url + "frmStudentMarksView.aspx"

        try:
            response = self.session.get(marks_url, timeout=10)
            response.raise_for_status()
        except Exception as exc:
            return {"success": False, "error": f"Failed to access marks page: {exc}"}

        if (
            "login.aspx" in response.url.lower()
            or "txtloginpassword" in response.text.lower()
        ):
            return {
                "success": False,
                "error": "University portal session expired while opening marks page.",
            }

        soup = BeautifulSoup(response.text, "html.parser")

        # Different UIMS campuses use different select names/IDs.
        select_tag = soup.find(
            "select",
            attrs={"name": re.compile(r"(ddlCAndPSession|ddlSession|Session)", re.I)},
        )

        if select_tag is None:
            select_tag = soup.find(
                "select",
                attrs={"id": re.compile(r"(ddlCAndPSession|ddlSession|Session)", re.I)},
            )

        # Last fallback: the marks page normally has only one select.
        if select_tag is None:
            selects = soup.find_all("select")
            if len(selects) == 1:
                select_tag = selects[0]

        if not select_tag:
            print(
                "[Marks] Dropdown missing; "
                f"status={response.status_code}, "
                f"url={response.url}, "
                f"title={soup.title.get_text(strip=True) if soup.title else ''}"
            )
            return {"success": False, "error": "Marks dropdown could not be found."}

        # ⭐ Result-type options (Final/Session) session IDs NAHI hain -
        # marks page ke select se leak hokar pool pollute karte the.
        _junk_opts = {"final", "session", "reg", "rep", "regular", "re-appear", "reappear"}
        available_sessions = [
            {
                "id": option.get("value", "").strip(),
                "name": option.get_text(" ", strip=True),
                "selected": option.has_attr("selected"),
            }
            for option in select_tag.find_all("option")
            if option.get("value", "").strip()
            and option.get("value", "").strip().lower() not in _junk_opts
        ]

        selected = select_tag.find("option", selected=True) or select_tag.find("option")
        default_session_id = selected.get("value", "").strip() if selected else ""
        session_id = str(session_id or default_session_id).strip()

        if not session_id:
            return {"success": False, "error": "Session term is empty."}

        accordion = soup.find("div", id="accordion")
        if accordion and session_id == default_session_id:
            result = self._parse_marks_html(soup, accordion)
            result["available_sessions"] = available_sessions
            result["active_session"] = session_id
            if not result.get("marks"):
                # Dump the raw page so we can see why the accordion is
                # empty (portal may now need a postback to fill it).
                try:
                    with open("marks_page_debug.html", "w", encoding="utf-8") as debug_file:
                        debug_file.write(response.text)
                except OSError:
                    pass
            return result

        # ⭐ Marks page pe sirf CURRENT session hota hai - puraane
        # semester (25262/25261) ka option uske dropdown me hi NAHI
        # hota. Aise session ka postback ASP.NET 500 ("Runtime Error")
        # phenkta hai (live console me pakda gaya). Aise case me 500
        # maare bina hi khaali-success do - sessionals ke absence me
        # result page ke Internal/External marks hi display ka source
        # bante hain.
        _option_ids = {
            str(item.get("id", "")).strip() for item in available_sessions
        }
        if (
            session_id != default_session_id
            and session_id not in _option_ids
        ):
            print(
                f"[Marks] session {session_id} marks-page options me "
                "nahi - skipping postback (500 se bache)"
            )
            return {
                "success": True,
                "marks": [],
                "available_sessions": available_sessions,
                "active_session": session_id,
            }

        data = self._hidden_fields(soup)
        data["__EVENTTARGET"] = select_tag.get("name")
        data["__EVENTARGUMENT"] = ""
        data[select_tag.get("name")] = session_id

        try:
            post_response = self.session.post(
                marks_url,
                data=data,
                cookies=self.session.cookies,
                timeout=10,
            )
            post_response.raise_for_status()
            post_soup = BeautifulSoup(post_response.text, "html.parser")
            post_accordion = post_soup.find("div", id="accordion")
            result = (
                self._parse_marks_html(post_soup, post_accordion)
                if post_accordion
                else {"success": True, "marks": []}
            )
            if isinstance(result, dict) and not result.get("marks"):
                try:
                    with open("marks_page_debug.html", "w", encoding="utf-8") as debug_file:
                        debug_file.write(post_response.text)
                except OSError:
                    pass
            result["available_sessions"] = available_sessions
            result["active_session"] = session_id
            return result
        except Exception as exc:
            return {"success": False, "error": f"Marks postback failed: {exc}"}

    def _parse_marks_html(self, soup, accordion):
        try:
            headings = [
                item.get_text(" ", strip=True)
                for item in accordion.find_all(["h1", "h2", "h3", "h4", "h5"])
            ]
            tables = accordion.find_all("table")
            parsed = []

            # The portal does not always place each subject inside a
            # direct child div. Pair headings and tables by position.
            for index, heading in enumerate(headings):
                course_code = ""
                course_title = heading

                if ":" in heading:
                    course_code, course_title = heading.split(":", 1)
                    course_code = course_code.strip()
                    course_title = course_title.strip()

                table = tables[index] if index < len(tables) else None
                table_text = table.get_text(" ", strip=True) if table else ""

                if not course_code:
                    code_match = re.search(
                        r"\b\d{2}[A-Z]{2,5}-\d{3}\b",
                        f"{heading} {table_text}",
                        flags=re.IGNORECASE,
                    )
                    if code_match:
                        course_code = code_match.group(0).upper()

                marks = []
                if table:
                    for row in table.find_all("tr"):
                        cells = row.find_all("td")
                        if len(cells) < 3:
                            continue

                        element = cells[0].get_text(" ", strip=True)
                        if "element" in element.lower() or "marks" in element.lower():
                            continue

                        marks.append({
                            "element": element,
                            "total": cells[1].get_text(" ", strip=True),
                            "obtained": cells[2].get_text(" ", strip=True),
                        })

                parsed.append({
                    "code": course_code,
                    "title": course_title,
                    "marks": marks,
                })

            # Some portal versions use h4/div headings instead of h3.
            # If no heading/table pairing was found, parse every table as
            # a subject block so current-session marks are not lost.
            if not parsed and tables:
                for table in tables:
                    table_text = table.get_text(" ", strip=True)
                    code_match = re.search(
                        r"\b\d{2}[A-Z]{2,5}-\d{3}\b",
                        table_text,
                        flags=re.IGNORECASE,
                    )
                    marks = []
                    for row in table.find_all("tr"):
                        cells = row.find_all("td")
                        if len(cells) < 3:
                            continue
                        element = cells[0].get_text(" ", strip=True)
                        if "element" in element.lower() or "marks" in element.lower():
                            continue
                        marks.append({
                            "element": element,
                            "total": cells[1].get_text(" ", strip=True),
                            "obtained": cells[2].get_text(" ", strip=True),
                        })
                    if marks:
                        parsed.append({
                            "code": code_match.group(0).upper() if code_match else "",
                            "title": code_match.group(0).upper() if code_match else "Subject",
                            "marks": marks,
                        })

            print(
                f"[Marks Parser] headings={len(headings)}, "
                f"tables={len(tables)}, records={len(parsed)}"
            )
            return {"success": True, "marks": parsed}
        except Exception as exc:
            return {"success": False, "error": f"Marks parsing exception: {exc}"}

    def scrape_exam_results(self, cookies_dict=None, sem_id=None, **kwargs):
        if cookies_dict:
            self.session.cookies = requests.utils.cookiejar_from_dict(cookies_dict)

        result_url = self.auth_url + "result.aspx"

        try:
            response = self.session.get(result_url, timeout=10)
            response.raise_for_status()
        except Exception as exc:
            return {"success": False, "error": f"Failed to access results ledger: {exc}"}

        if (
            "login.aspx" in response.url.lower()
            or "txtloginpassword" in response.text.lower()
        ):
            return {
                "success": False,
                "error": "University portal session expired while opening results page.",
            }

        soup = BeautifulSoup(response.text, "html.parser")

        # ⭐ INITIAL (GET) page preserve karo. Reveal/switch postbacks ke
        # baad `soup` badal jaata hai, PAR live pages ne prove kiya ki
        # SGPA-history text ("Semester : N SGPA : x") SIRF initial page
        # pe hoti hai - post-reveal page pe sgpa_rows=0 aata tha, jisse
        # declared-results khaali ho ke Sem 3 dropdown me leak hota tha.
        initial_soup = soup

        # ⭐ DEBUG: page ke SAB dropdowns ka map. Live campus pe session
        # select ka naam/values alag ho sakte hain - agar pool me puraane
        # sessions (25262/25261) na aayein to INHI lines se pata chalega
        # ki asli select kaunsa hai. Console ke ye lines bhej dena!
        _all_selects = soup.find_all("select")
        for _i, _sel in enumerate(_all_selects):
            _opts = _sel.find_all("option")
            _vals = [
                "{}={}".format(
                    (o.get("value") or "").strip(),
                    o.get_text(" ", strip=True)[:14],
                )
                for o in _opts[:4]
            ]
            print(
                f"[Results] select#{_i} name={_sel.get('name') or '-'} "
                f"opts={len(_opts)} {_vals}"
            )

        # ⭐ SESSION dropdown ka SCORED selection. Real result.aspx pe
        # TEEN dropdowns hain: ddlResultType (Final/Session), ddlSession
        # (May-2026 -> "25262"), ddlCategory (REG/REP). Galat dropdown
        # (ResultType) pe postback karne se Sem 1/2 ke subjects aapas
        # me swap ho jaate the. Rules:
        #   - name/id me ResultType/Category  -> KABHI NAHI (score -1)
        #   - name/id me ddlSemester/ddlSession/ddlTerm -> +100
        #   - option value 4-6 digit numeric (real session id) -> +10
        def _session_select_score(tag):
            name_id = "{} {}".format(tag.get("name", ""), tag.get("id", ""))
            if re.search(r"(resulttype|category)", name_id, re.I):
                return -1
            score = 0
            if re.search(r"(ddlsemester|ddlsession|ddlterm)", name_id, re.I):
                score += 100
            for opt in tag.find_all("option"):
                if re.fullmatch(r"\d{4,6}", (opt.get("value") or "").strip()):
                    score += 10
            return score

        select_tag = None
        best_score = 0
        for _candidate in _all_selects:
            _score = _session_select_score(_candidate)
            if _score > best_score:
                select_tag = _candidate
                best_score = _score

        # ⭐ RESCUE: score 0 reh gaya (naam/values anjaane) to bhi
        # junk-excluded selects me SABSE ZYADA options wale ko session
        # select maano - dropdown kabhi khaali na rahe.
        if select_tag is None:
            _rescue = [
                s
                for s in _all_selects
                if s.find_all("option")
                and not re.search(
                    r"(resulttype|category)",
                    "{} {}".format(s.get("name", ""), s.get("id", "")),
                    re.I,
                )
            ]
            if _rescue:
                _rescue.sort(
                    key=lambda s: len(s.find_all("option")),
                    reverse=True,
                )
                select_tag = _rescue[0]
                print(
                    "[Results] RESCUE select:",
                    select_tag.get("name") or select_tag.get("id"),
                )

        # ⭐⭐ MARKS-VIEW INFRA (v5.0 - marks-merge):
        # Live results_debug.html ne PROVE kar diya: initial (Final-type)
        # result page ki table sirf 4-column hai - Subject Code | Subject
        # Name | Credits | Grade (Internal/External NAHI). Marks SIRF
        # ddlResultType = "Session" toggle ke response (6-column: Code |
        # Name | Internal Marks | External Marks | Credits | Grade - portal
        # screenshot confirm) me aate hain. Wo response pehle sirf session
        # dropdown dhundhne ke liye hota tha aur DISCARD ho jaata tha -
        # ab capture karte hain aur neeche code-wise MERGE karte hain.
        _marks_soups = []

        def _soup_has_marks_page(s):
            if s is None:
                return False
            for _t in s.find_all("table"):
                # ⭐ Wrapper/outer tables (nested <table> inside) SKIP:
                # unke header-row ka find_all nested th bhi uthata hai,
                # aur ek giant-cell jo "internal" word rakhta hai wo
                # column-index 0 ban jaata hai -> code cell marks me
                # ghus jaata hai (harness M2 ne pakra). Asli marks
                # tables ke andar kabhi nested table nahi hoti.
                if _t.find("table") is not None:
                    continue
                _hdrs = _t.find_all("tr")[:1]
                if not _hdrs:
                    continue
                _htxt = " ".join(
                    _h.get_text(" ", strip=True).lower()
                    for _h in _hdrs[0].find_all(["th", "td"])
                )
                if "internal" in _htxt and "external" in _htxt:
                    return True
            return False

        # ⭐⭐ RESULT-TYPE REVEAL (LIVE page ka asli behaviour):
        # Initial GET pe result page SIRF ddlResultType (Final/Session)
        # bhejta hai - SESSION dropdown (ddlSession: May-2026->25262)
        # uska AutoPostBack ke BAAD render hota hai (ASP.NET dependent
        # dropdowns). AUR server tabhi dependent controls deta hai jab
        # value CHANGE ho - SAME selected value post karne pe "koi
        # change nahi" maan ke wahi minimal page lautata hai (live
        # console ne ye prove kiya: Session->Session pe selects=1 raha).
        # Isliye reveal ke liye NON-SELECTED value pehle try karo:
        # Final selected ho to Session post karo, aur vice versa.
        if select_tag is None:
            _rt_tag = None
            for _s in _all_selects:
                _n_id = "{} {}".format(_s.get("name", ""), _s.get("id", ""))
                if (
                    re.search(r"resulttype", _n_id, re.I)
                    and _s.find_all("option")
                    and _s.get("name")
                ):
                    _rt_tag = _s
                    break
            if _rt_tag is not None:
                _rt_name = _rt_tag.get("name")
                _rt_sel = (
                    _rt_tag.find("option", selected=True)
                    or _rt_tag.find("option")
                )
                _rt_selected = (
                    (_rt_sel.get("value") or "").strip() if _rt_sel else ""
                )
                # ⭐ Non-selected PEHLE - change-trigger reveal ke liye
                _rt_candidates = [
                    (o.get("value") or "").strip()
                    for o in _rt_tag.find_all("option")
                    if (o.get("value") or "").strip()
                ]
                _rt_order = (
                    [v for v in _rt_candidates if v != _rt_selected]
                    + [_rt_selected]
                )
                for _rt_val in _rt_order:
                    try:
                        _data = self._hidden_fields(soup)
                        for _sx in soup.find_all("select"):
                            _nx = _sx.get("name")
                            if not _nx:
                                continue
                            _ox = (
                                _sx.find("option", selected=True)
                                or _sx.find("option")
                            )
                            _data[_nx] = _ox.get("value", "") if _ox else ""
                        for _bx in soup.find_all("input", type="submit"):
                            _bn = _bx.get("name")
                            if _bn:
                                _data[_bn] = _bx.get("value", "")
                        _data["__EVENTTARGET"] = _rt_name
                        _data["__EVENTARGUMENT"] = ""
                        _data[_rt_name] = _rt_val

                        _resp = self.session.post(
                            result_url,
                            data=_data,
                            cookies=self.session.cookies,
                            timeout=15,
                        )
                        _resp.raise_for_status()
                        _soup2 = BeautifulSoup(_resp.text, "html.parser")
                        _reveal_selects = _soup2.find_all("select")
                        print(
                            f"[Results] REVEAL try={_rt_val} -> "
                            f"selects={len(_reveal_selects)}"
                        )
                        for _i, _sel in enumerate(_reveal_selects):
                            _opts = _sel.find_all("option")
                            _vals = [
                                "{}={}".format(
                                    (o.get("value") or "").strip(),
                                    o.get_text(" ", strip=True)[:14],
                                )
                                for o in _opts[:4]
                            ]
                            print(
                                f"[Results] select#{_i}* "
                                f"name={_sel.get('name') or '-'} "
                                f"opts={len(_opts)} {_vals}"
                            )

                        # ⭐ v5.0: yahi Session-toggle response Internal/
                        # External marks laata hai - CAPTURE karo.
                        # Pehle ye page dropdown reveal ke baad discard
                        # ho jaata tha, isliye screen pe int/ext KABHI
                        # nahi dikhte the (live results_debug.html me
                        # sirf 4-col table hai).
                        if _soup_has_marks_page(_soup2):
                            _marks_soups.append(_soup2)
                            print(
                                f"[Results] MARKS-view captured "
                                f"(result-type={_rt_val})"
                            )

                        # Revealed page pe phir se scored selection
                        _sel2 = None
                        _sc2 = 0
                        for _c2 in _reveal_selects:
                            _s2sc = _session_select_score(_c2)
                            if _s2sc > _sc2:
                                _sel2 = _c2
                                _sc2 = _s2sc
                        if _sel2 is None:
                            _rescue2 = [
                                s
                                for s in _reveal_selects
                                if s.find_all("option")
                                and not re.search(
                                    r"(resulttype|category)",
                                    "{} {}".format(
                                        s.get("name", ""), s.get("id", "")
                                    ),
                                    re.I,
                                )
                            ]
                            if _rescue2:
                                _rescue2.sort(
                                    key=lambda s: len(s.find_all("option")),
                                    reverse=True,
                                )
                                _sel2 = _rescue2[0]
                        # Har try ka page agle attempt ka base banao
                        # (updated __VIEWSTATE/__EVENTVALIDATION).
                        soup = _soup2
                        if _sel2 is not None:
                            select_tag = _sel2
                            print(
                                "[Results] REVEAL ok - session select:",
                                select_tag.get("name")
                                or select_tag.get("id"),
                            )
                            break
                    except Exception as _exc:
                        print(f"[Results] REVEAL try={_rt_val} failed: {_exc}")

        available_sems = []
        if select_tag:
            available_sems = [
                {
                    "id": option.get("value", "").strip(),
                    "name": option.get_text(" ", strip=True),
                    "selected": option.has_attr("selected"),
                }
                for option in select_tag.find_all("option")
            ]

        selected = None
        if select_tag:
            selected = select_tag.find("option", selected=True) or select_tag.find("option")

        default_sem_id = selected.get("value", "").strip() if selected else ""
        requested_sem_id = str(sem_id or "").strip()
        matching_option = None

        if select_tag and requested_sem_id:
            for option in select_tag.find_all("option"):
                value = option.get("value", "").strip()
                text = option.get_text(" ", strip=True).lower()

                if value == requested_sem_id:
                    matching_option = option
                    break

                number_match = re.search(
                    r"\b(?:semester|sem)\s*[-:]?\s*(\d+)\b",
                    text,
                    flags=re.I,
                )
                if number_match and number_match.group(1) == requested_sem_id:
                    matching_option = option
                    break

        if matching_option is not None:
            sem_id = matching_option.get("value", "").strip()
        else:
            sem_id = requested_sem_id or default_sem_id

        # ⭐⭐ Semester-switch ka ALAG postback NAHI chahiye!
        # Live pages ne prove kar diya:
        #   - INITIAL (Final-type) page pe DONO semesters ki REAL grade
        #     tables maujud hoti hain (v4.1 logs: tables=3, sgpa_rows=2)
        #   - ddlSession/ddlResultType postback ka response SESSIONAL
        #     view hota hai (tables=2, marks-type columns) - wahan se
        #     parse karne pe "marks & grade sab GALAT" dikhta tha
        #     (tumhari report).
        # Isliye DATA hamesha initial page se parse hota hai; reveal
        # sirf session DROPDOWN ke liye tha (available_sems).

        # ⭐ HAMESHA initial result page ka dump rakho (data-source of
        # truth). SGPA/CGPA/subjects ka format yahi se confirm hota
        # hai - parse galat aaye to ye file bhejo, exact wiring hogi.
        try:
            with open("results_debug.html", "w", encoding="utf-8") as fh:
                fh.write(initial_soup.prettify())
        except OSError:
            pass

        # ⭐ SGPA-history/global/active SAB initial (Final-type) page se -
        # reveal/switch page pe history text aur real grade columns dono
        # nahi hote (sessional view hota hai).
        page_text = initial_soup.get_text(" ", strip=True)
        history_text = page_text

        # ⭐ Requested semester EARLY resolve - table-selection,
        # strict-pending aur active-SGPA fallback teeno isi se chalte.
        resolved_sem_num = kwargs.get("semester_number")
        try:
            resolved_sem_num = (
                int(resolved_sem_num) if resolved_sem_num else None
            )
        except (TypeError, ValueError):
            resolved_sem_num = None
        if resolved_sem_num is None and sem_id is not None:
            try:
                _rsn = int(str(sem_id))
                if 1 <= _rsn <= 12:
                    resolved_sem_num = _rsn
            except (TypeError, ValueError):
                pass

        # ⭐ Ongoing semester (Sem 3) jiska result page pe table hi nahi
        # - strict-pending flag; template "declared nahi hua" dikhata hai.
        semester_pending = False

        parsed_results = []
        seen_semesters = set()

        sgpa_matches = re.findall(
            r"(?:Semester|Sem)\s*[:\-]?\s*(\d+)"
            r"\s+SGPA\s*[:\-]?\s*"
            r"([0-9]+(?:\.[0-9]+)?)",
            history_text,
            flags=re.I,
        )

        for semester_number, sgpa in sgpa_matches:
            if semester_number in seen_semesters:
                continue
            seen_semesters.add(semester_number)
            parsed_results.append({
                "semester": f"Semester {semester_number}",
                "sgpa": sgpa,
                "cgpa": sgpa,
            })

        parsed_results.sort(
            key=lambda item: int(re.search(r"\d+", item["semester"]).group())
        )

        # ⭐ FALLBACK: "Semester N SGPA x" text-regex khaali gaya ho to
        # TABLE-format history parse karo (header me SGPA column) - kai
        # UIMS pages summary ko table me dete hain, plain text me nahi.
        if not parsed_results:
            for table in initial_soup.find_all("table"):
                rows = table.find_all("tr")
                if len(rows) < 2:
                    continue
                header = [
                    cell.get_text(" ", strip=True).lower()
                    for cell in rows[0].find_all(["th", "td"])
                ]
                sgpa_col = next(
                    (i for i, h in enumerate(header) if "sgpa" in h), None
                )
                if sgpa_col is None:
                    continue
                sem_col = next(
                    (i for i, h in enumerate(header)
                     if "sem" in h or "session" in h or "term" in h),
                    None,
                )
                cgpa_col = next(
                    (i for i, h in enumerate(header) if "cgpa" in h), None
                )
                for row in rows[1:]:
                    cells = [
                        cell.get_text(" ", strip=True)
                        for cell in row.find_all(["th", "td"])
                    ]
                    if sgpa_col >= len(cells):
                        continue
                    sgpa_val = re.search(
                        r"[0-9]+(?:\.[0-9]+)?", cells[sgpa_col]
                    )
                    if not sgpa_val:
                        continue
                    sem_num = None
                    row_text = " ".join(cells)
                    sem_m = re.search(
                        r"(?:semester|sem)\s*[-:]?\s*(\d+)", row_text, re.I
                    )
                    if sem_m:
                        sem_num = int(sem_m.group(1))
                    elif sem_col is not None and sem_col < len(cells):
                        num_m = re.search(r"(\d+)", cells[sem_col])
                        if num_m and int(num_m.group(1)) <= 12:
                            sem_num = int(num_m.group(1))
                    if sem_num is None or str(sem_num) in seen_semesters:
                        continue
                    seen_semesters.add(str(sem_num))
                    entry = {
                        "semester": f"Semester {sem_num}",
                        "sgpa": sgpa_val.group(0),
                        "cgpa": sgpa_val.group(0),
                    }
                    if cgpa_col is not None and cgpa_col < len(cells):
                        cm = re.search(
                            r"[0-9]+(?:\.[0-9]+)?", cells[cgpa_col]
                        )
                        if cm:
                            entry["cgpa"] = cm.group(0)
                    parsed_results.append(entry)
                if parsed_results:
                    break
            parsed_results.sort(
                key=lambda item: int(
                    re.search(r"\d+", item["semester"]).group()
                )
            )

        cgpa_match = re.search(
            r"(?:CGPA|Cumulative\s+GPA)\s*[:\-]?\s*"
            r"([0-9]+(?:\.[0-9]+)?)",
            history_text,
            flags=re.I,
        )

        # ⭐ Guard: table-text flow me "CGPA 1 8.91" jaisa sequence
        # semester-NUMBER (1) ko CGPA samajh le - isliye sirf > 1.0
        # values hi asli SGPA/CGPA maano (GPA scale 4-10 hota hai).
        if cgpa_match and float(cgpa_match.group(1)) > 1.0:
            global_cgpa = cgpa_match.group(1)
        elif parsed_results:
            global_cgpa = parsed_results[-1]["sgpa"]
        elif cgpa_match:
            global_cgpa = cgpa_match.group(1)
        else:
            global_cgpa = "0.00"

        # ⭐ LATEST declared semester ka cumulative CGPA = portal ka
        # global CGPA hi hota hai (e.g. Sem 2 row ka CGPA 6.98, sirf
        # uska SGPA 6.92 NAHI). History parse (text ya table-dono) me
        # per-row CGPA na mile to cgpa=sgpa fallback aa jata tha -
        # isliye Sem 2 view pe 6.92 dikh raha tha (live console fix).
        # Last row ka CGPA hamesha global se theek karo.
        if parsed_results:
            try:
                if float(global_cgpa) > 1.0:
                    parsed_results[-1]["cgpa"] = global_cgpa
            except (TypeError, ValueError):
                pass

        # ⭐ ACTIVE semester ka SGPA/CGPA text se (page pe visible card) -
        # history list khaali ho tab bhi views isse display kar sakta hai.
        active_sgpa_text = ""
        for sgpa_any in re.finditer(
            r"SGPA\s*[:\-]?\s*([0-9]+(?:\.[0-9]+)?)", page_text, re.I
        ):
            if float(sgpa_any.group(1)) > 1.0:
                active_sgpa_text = sgpa_any.group(1)
                break
        active_cgpa_text = ""
        for cgpa_any in re.finditer(
            r"(?:CGPA|Cumulative\s+GPA)\s*[:\-]?\s*([0-9]+(?:\.[0-9]+)?)",
            page_text,
            re.I,
        ):
            if float(cgpa_any.group(1)) > 1.0:
                active_cgpa_text = cgpa_any.group(1)
                break

        # ⭐ Active SGPA requested semester KE HISAB SE history row se
        # (initial page text pe sirf LATEST semester ka card hota hai -
        # Sem 3 ya Sem 1 request pe bhi 6.92 ka STALE text aa raha tha,
        # live run me pakda gaya). resolved semester NAHI ho to hi
        # text-found value final hoti hai.
        if resolved_sem_num is not None:
            _hist_match = ""
            for _hist in parsed_results:
                if _hist.get("semester") == (
                    f"Semester {resolved_sem_num}"
                ):
                    _hist_match = _hist.get("sgpa", "")
                    active_cgpa_text = (
                        _hist.get("cgpa", "") or active_cgpa_text
                    )
                    break
            active_sgpa_text = _hist_match

        # Parse ONLY the selected semester's subject table - and ALWAYS
        # from the initial (Final-type) page, jahan dono semesters ki
        # grade tables hoti hain.
        subject_grades = []
        candidate_tables = []

        for table in initial_soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue

            headers = [
                cell.get_text(" ", strip=True).lower()
                for cell in rows[0].find_all(["th", "td"])
            ]

            has_code = any("code" in header for header in headers)
            has_subject = any("subject" in header for header in headers)

            if has_code and has_subject:
                candidate_tables.append(table)

        # --- Robust table -> semester mapping -------------------------
        # Walk the document in source order and tag every candidate table
        # with the most recent "Semester N" label that precedes it. This
        # removes the fragile hardcoded index assumption that was mixing
        # up Semester 1 and Semester 2.
        table_semester = {}
        current_semester = None
        for element in initial_soup.find_all(
            ["h1", "h2", "h3", "h4", "h5", "b", "strong", "span", "div", "table"]
        ):
            if element.name == "table":
                if element in candidate_tables:
                    table_semester[element] = current_semester
                continue
            label = element.get_text(" ", strip=True)
            sem_match = re.search(
                r"(?:Semester|Sem)\s*[-:]?\s*(\d+)",
                label,
                flags=re.I,
            )
            if sem_match:
                current_semester = int(sem_match.group(1))

        marks_codes = {
            str(code).strip().upper()
            for code in kwargs.get("marks_codes", [])
            if str(code).strip()
        }

        # The result page often renders an aggregate/combined grade table
        # that contains EVERY semester's subjects on top of the per-semester
        # tables. Selecting that combined table mixes Semester 1 and
        # Semester 2. Drop any table whose subject codes are a strict
        # superset of another candidate table's codes - those are the
        # combined views.
        def _table_code_set(table):
            codes = set()
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if cells:
                    code = cells[0].get_text(" ", strip=True).upper()
                    if re.match(r"^\d{2}[A-Z]{2,5}-\d{3}$", code):
                        codes.add(code)
            return codes

        table_code_sets = {id(t): _table_code_set(t) for t in candidate_tables}
        specific_tables = []
        for t in candidate_tables:
            tc = table_code_sets[id(t)]
            is_combined = False
            for u in candidate_tables:
                if u is t:
                    continue
                uc = table_code_sets[id(u)]
                if uc and uc <= tc and len(uc) < len(tc):
                    is_combined = True
                    break
            if not is_combined:
                specific_tables.append(t)

        selected_table = None

        # 1) Code-overlap with the active session's sessional marks.
        #    Most reliable: pick the per-semester table that actually
        #    contains this session's subject codes. On equal overlap we
        #    prefer the SMALLER table so a combined view is never chosen.
        if marks_codes:
            best_table = None
            best_overlap = 0
            for table in specific_tables:
                overlap = len(table_code_sets[id(table)] & marks_codes)
                smaller = (
                    best_table is None
                    or len(table_code_sets[id(table)])
                    < len(table_code_sets[id(best_table)])
                )
                if overlap > best_overlap or (overlap == best_overlap and smaller):
                    best_overlap = overlap
                    best_table = table
            if best_table is not None and best_overlap > 0:
                selected_table = best_table
                print(
                    f"[Results] Code-overlap selected table, "
                    f"overlap={best_overlap}"
                )

        # 2) Exact semester label match. ⭐ VIEWS ab session id ka
        #    academic number (1..8) `semester_number` kwarg me bhejta
        #    hai - resolved_sem_num upar hi compute ho chuka hai.
        #    ⭐⭐ SIRF specific_tables me se choose karo: Final-type page
        #    pe Semester-2 block ki table CUMULATIVE hoti hai (Sem 1+2
        #    milake 17 rows) - wo combined view pehle hi drop ho chuki
        #    hai, par label-walk usi ko "2" tag karke lata tha (live
        #    console: sem=2 pe subjects=17 + Sem-1 codes + INT/EXT gayab).
        if selected_table is None and resolved_sem_num is not None:
            _label_matches = [
                table
                for table, sem in table_semester.items()
                if sem == resolved_sem_num and table in specific_tables
            ]
            if _label_matches:
                # ⭐ Ek se zyada ho to SABSE CHHOTI (combined view kabhi
                # na chune jaye - doosri safety line).
                _label_matches.sort(
                    key=lambda t: len(table_code_sets[id(t)])
                )
                selected_table = _label_matches[0]
                print(
                    f"[Results] Sem-label selected table, "
                    f"sem={resolved_sem_num} "
                    f"rows={len(table_code_sets[id(selected_table)])}"
                )

        # 3) STRICT PENDING vs fallback.
        #    Page pe semester LABELS hain AUR requested semester ki table
        #    NAHI mili -> wo semester (Sem 3, chal raha hai) ka result
        #    portal pe DECLARE hi nahi hua. Aise me SUBJECTS KHAALI hi
        #    rakhna sahi hai - Sem 2 ki table "Semester 3" ke neeche
        #    dikhana user ko confuse karta hai (user ne pakda).
        #    Labels hi NAHI mili (single/odd page) to fallback chalta hai.
        if selected_table is None and specific_tables:
            labelled = [
                (sem, table)
                for table, sem in table_semester.items()
                if table in specific_tables and sem is not None
            ]
            if labelled and (
                resolved_sem_num is not None
                and resolved_sem_num
                not in {sem for sem, _ in labelled}
            ):
                semester_pending = True
                print(
                    f"[Results] Sem {resolved_sem_num} NOT DECLARED - "
                    "pending (no table on page)"
                )
            elif labelled:
                labelled.sort(key=lambda pair: pair[0])
                selected_table = labelled[-1][1]
                print(
                    f"[Results] Fallback newest table, "
                    f"sem={labelled[-1][0]}"
                )
            else:
                selected_table = specific_tables[-1]

        # ⭐ Table-parser ko closure banao taaki grade-sanity rescue me
        # kisi bhi table pe chala sakein. Columns HEADER se map hote hain
        # (real 6-column page: Subject Code | Subject Name | Internal
        # Marks | External Marks | Credits | Grade - screenshot confirm).
        def _parse_table_grades(table):
            rows_out = []
            seen_codes = set()

            header_row = table.find_all("tr")[0]
            header_texts = [
                h.get_text(" ", strip=True).lower()
                for h in header_row.find_all(["th", "td"])
            ]

            def _col(*needles):
                for _i, _t in enumerate(header_texts):
                    if all(n in _t for n in needles):
                        return _i
                return None

            idx_code = _col("code")
            if idx_code is None:
                idx_code = 0
            idx_subject = _col("name")
            if idx_subject is None or idx_subject == idx_code:
                idx_subject = next(
                    (
                        _i
                        for _i, _t in enumerate(header_texts)
                        if _i != idx_code and "subject" in _t
                    ),
                    None,
                )
            if idx_subject is None:
                idx_subject = 1
            idx_internal = _col("internal")
            idx_external = _col("external")
            idx_credits = _col("credit")
            if idx_credits is None:
                idx_credits = 2
            idx_grade = _col("grade")
            if idx_grade is None:
                idx_grade = len(header_texts) - 1 if header_texts else 3

            def _cell(cells, idx):
                if idx is None or idx >= len(cells):
                    return ""
                return cells[idx].get_text(" ", strip=True)

            for row in table.find_all("tr")[1:]:
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue

                code = _cell(cells, idx_code)
                code_lower = code.lower()

                if (
                    not code
                    or "code" in code_lower
                    or "subject" in code_lower
                ):
                    continue

                code_key = code.upper()
                if code_key in seen_codes:
                    continue
                seen_codes.add(code_key)

                grade = _cell(cells, idx_grade)
                # Safety: grade cell me letter grade NAHI hai to row ke
                # END se grade-pattern wali value uthao (kabhi header
                # alag order me aaye to bhi grade sahi rahe).
                if grade and not re.search(r"[A-DF]", grade.upper()):
                    for _c in reversed(cells):
                        _t = _c.get_text(" ", strip=True)
                        if re.fullmatch(r"(?i)(O|[A-F][+]?|AB)", _t):
                            grade = _t
                            break

                rows_out.append({
                    "code": code,
                    "title": _cell(cells, idx_subject),
                    # ⭐ Result page ke Internal/External marks - sessional
                    # marks ka fallback display isi se chalega.
                    "internal": _cell(cells, idx_internal),
                    "external": _cell(cells, idx_external),
                    "credits": _cell(cells, idx_credits),
                    "grade": grade,
                })
            return rows_out

        # ⭐ GRADE-SANITY SELF-HEAL: chuni hui table ke parsed grades ka
        # health score (kitne % rows me asli letter-grade B/C+/A hai).
        # Wrapper/combined/wrong table slip kar gayi ho to isse pakad lo.
        def _grade_health(rows_out):
            if not rows_out:
                return 0.0
            ok = sum(
                1
                for s in rows_out
                if re.fullmatch(
                    r"(?i)(O|[A-F][+]?|AB)", (s.get("grade") or "").strip()
                )
            )
            return ok / len(rows_out)

        if selected_table is not None:
            subject_grades = _parse_table_grades(selected_table)
            health = _grade_health(subject_grades)
            print(
                f"[Results] grade-sanity {health:.0%} "
                f"({len(subject_grades)} rows)"
            )

            if health < 0.6 and specific_tables:
                # ⭐ Rescue: label-matched tables pehle, phir chhoti
                # tables - jiska health sabse accha ho wahi asli result
                # table hai.
                _alternates = sorted(
                    specific_tables,
                    key=lambda t: (
                        0
                        if table_semester.get(t) == resolved_sem_num
                        else 1,
                        len(table_code_sets[id(t)]) or 999,
                    ),
                )
                for _alt in _alternates:
                    if _alt is selected_table:
                        continue
                    _alt_rows = _parse_table_grades(_alt)
                    _alt_health = _grade_health(_alt_rows)
                    if _alt_rows and _alt_health > health:
                        print(
                            "[Results] grade-sanity RESCUE -> "
                            f"health {health:.0%} to {_alt_health:.0%}"
                        )
                        selected_table = _alt
                        subject_grades = _alt_rows
                        health = _alt_health
                        break

        # ⭐⭐ INTERNAL/EXTERNAL MARKS MERGE (v5.0 - marks-merge)
        # Initial (Final-type) page ki grade table 4-column hai (live
        # results_debug.html confirm); Internal/External SIRF Session-
        # toggle ke 6-col view me aate hain. Grade rows ke saath code-
        # wise merge karo. Kuch na mile to KOI REGRESSION NAHI - int/ext
        # blank rehte hain, views grade-fallback dikhata hai, baaki sab
        # bilkul same rehta hai.
        if selected_table is not None and subject_grades:
            try:
                _needs_marks = not any(
                    (
                        str(_s.get("internal") or "").strip()
                        or str(_s.get("external") or "").strip()
                    )
                    for _s in subject_grades
                )

                if _needs_marks and not _marks_soups:
                    # FALLBACK HUNT: reveal loop nahi chal paya ya us
                    # toggle ne marks-view na diya - initial page se ek
                    # alag dedicated toggle try (reveal jaisa hi post).
                    _rt2 = None
                    for _s2 in initial_soup.find_all("select"):
                        _nid2 = "{} {}".format(
                            _s2.get("name", ""), _s2.get("id", "")
                        )
                        if (
                            re.search(r"resulttype", _nid2, re.I)
                            and _s2.get("name")
                            and _s2.find_all("option")
                        ):
                            _rt2 = _s2
                            break
                    if _rt2 is not None:
                        _rt2_name = _rt2.get("name")
                        _rt2_sel = (
                            _rt2.find("option", selected=True)
                            or _rt2.find("option")
                        )
                        _rt2_cur = (
                            (_rt2_sel.get("value") or "").strip()
                            if _rt2_sel
                            else ""
                        )
                        for _o2 in _rt2.find_all("option"):
                            _v2 = (_o2.get("value") or "").strip()
                            if not _v2 or _v2 == _rt2_cur:
                                continue
                            try:
                                _d2 = self._hidden_fields(initial_soup)
                                for _sx2 in initial_soup.find_all("select"):
                                    _nx2 = _sx2.get("name")
                                    if not _nx2:
                                        continue
                                    _ox2 = (
                                        _sx2.find("option", selected=True)
                                        or _sx2.find("option")
                                    )
                                    _d2[_nx2] = (
                                        _ox2.get("value", "")
                                        if _ox2
                                        else ""
                                    )
                                for _bx2 in initial_soup.find_all(
                                    "input", type="submit"
                                ):
                                    _bn2 = _bx2.get("name")
                                    if _bn2:
                                        _d2[_bn2] = _bx2.get("value", "")
                                _d2["__EVENTTARGET"] = _rt2_name
                                _d2["__EVENTARGUMENT"] = ""
                                _d2[_rt2_name] = _v2
                                _r2 = self.session.post(
                                    result_url,
                                    data=_d2,
                                    cookies=self.session.cookies,
                                    timeout=15,
                                )
                                if _r2.status_code != 200:
                                    continue
                                _ms2 = BeautifulSoup(
                                    _r2.text, "html.parser"
                                )
                                if _soup_has_marks_page(_ms2):
                                    _marks_soups.append(_ms2)
                                    print(
                                        f"[Results] MARKS-view captured "
                                        f"(fallback type={_v2})"
                                    )
                                    break
                            except Exception as _e2:
                                print(
                                    f"[Results] marks toggle {_v2} "
                                    f"failed: {_e2}"
                                )

                # PER-SESSION SWEEP: marks-view sirf ek semester ka ho
                # sakta hai - usme ddlSession mila to requested semester
                # (pehle) + baaki sessions ke marks bhi uthao (cap 3).
                if _needs_marks and _marks_soups:
                    _m_base = _marks_soups[-1]
                    _m_sel = None
                    _m_sc = 0
                    for _c3 in _m_base.find_all("select"):
                        _sc3 = _session_select_score(_c3)
                        if _sc3 > _m_sc:
                            _m_sel = _c3
                            _m_sc = _sc3
                    if _m_sel is not None and _m_sel.get("name"):
                        _m_name = _m_sel.get("name")
                        _m_opts = [
                            (_o3.get("value") or "").strip()
                            for _o3 in _m_sel.find_all("option")
                            if (_o3.get("value") or "").strip()
                        ]
                        _m_selcur = (
                            _m_sel.find("option", selected=True)
                            or _m_sel.find("option")
                        )
                        _m_cur = (
                            (_m_selcur.get("value") or "").strip()
                            if _m_selcur
                            else ""
                        )
                        _want = []
                        if sem_id is not None and str(sem_id) in _m_opts:
                            _want.append(str(sem_id))
                        _want += [
                            v
                            for v in reversed(_m_opts)
                            if v not in _want and v != _m_cur
                        ]
                        for _sid3 in _want[:3]:
                            try:
                                _d3 = self._hidden_fields(_m_base)
                                for _sx3 in _m_base.find_all("select"):
                                    _nx3 = _sx3.get("name")
                                    if not _nx3:
                                        continue
                                    _ox3 = (
                                        _sx3.find("option", selected=True)
                                        or _sx3.find("option")
                                    )
                                    _d3[_nx3] = (
                                        _ox3.get("value", "")
                                        if _ox3
                                        else ""
                                    )
                                for _bx3 in _m_base.find_all(
                                    "input", type="submit"
                                ):
                                    _bn3 = _bx3.get("name")
                                    if _bn3:
                                        _d3[_bn3] = _bx3.get("value", "")
                                _d3["__EVENTTARGET"] = _m_name
                                _d3["__EVENTARGUMENT"] = ""
                                _d3[_m_name] = _sid3
                                _r3 = self.session.post(
                                    result_url,
                                    data=_d3,
                                    cookies=self.session.cookies,
                                    timeout=15,
                                )
                                if _r3.status_code != 200:
                                    print(
                                        f"[Results] marks session {_sid3}"
                                        f" HTTP {_r3.status_code} - skip"
                                    )
                                    continue
                                _ms3 = BeautifulSoup(
                                    _r3.text, "html.parser"
                                )
                                # ⭐ Agla attempt isi page ka VIEWSTATE
                                # use kare (ASP.NET postback chain).
                                _m_base = _ms3
                                if _soup_has_marks_page(_ms3):
                                    _marks_soups.append(_ms3)
                                    print(
                                        f"[Results] MARKS-view captured "
                                        f"(session {_sid3})"
                                    )
                            except Exception as _e3:
                                print(
                                    f"[Results] marks session {_sid3} "
                                    f"failed: {_e3}"
                                )

                if _needs_marks and _marks_soups:
                    _marks_map = {}

                    def _marks_val(v):
                        # ⭐ Junk-guard: asli marks hamesha numeric
                        # hote hain (58.21 / 18.00). Kisi weird table
                        # ne code/text de diya to drop - koi galat
                        # value merge NAHI hogi.
                        v = str(v or "").strip()
                        return (
                            v
                            if re.fullmatch(r"\d{1,3}(?:\.\d+)?", v)
                            else ""
                        )

                    for _ms4 in _marks_soups:
                        for _t4 in _ms4.find_all("table"):
                            # Wrapper tables skip (same nested-table bug)
                            if _t4.find("table") is not None:
                                continue
                            for _row4 in _parse_table_grades(_t4):
                                _i4 = _marks_val(_row4.get("internal"))
                                _e4 = _marks_val(_row4.get("external"))
                                if not (_i4 or _e4):
                                    continue
                                _k4 = (
                                    str(_row4.get("code") or "")
                                    .strip()
                                    .upper()
                                )
                                if not _k4:
                                    continue
                                if _k4 not in _marks_map or not any(
                                    _marks_map[_k4]
                                ):
                                    _marks_map[_k4] = (_i4, _e4)
                    _merged = 0
                    for _s5 in subject_grades:
                        _k5 = str(_s5.get("code") or "").strip().upper()
                        _p5 = _marks_map.get(_k5)
                        if not _p5:
                            continue
                        if _p5[0] and not str(
                            _s5.get("internal") or ""
                        ).strip():
                            _s5["internal"] = _p5[0]
                        if _p5[1] and not str(
                            _s5.get("external") or ""
                        ).strip():
                            _s5["external"] = _p5[1]
                        if _p5[0] or _p5[1]:
                            _merged += 1
                    print(
                        f"[Results] marks-merge {_merged}/"
                        f"{len(subject_grades)} rows "
                        f"(views={len(_marks_soups)} "
                        f"codes={len(_marks_map)})"
                    )
                    # ⭐ Marks-view dump - agar merge 0 aaye to isi file
                    # se asli structure confirm hoga.
                    try:
                        with open(
                            "results_marks_debug.html",
                            "w",
                            encoding="utf-8",
                        ) as _fh:
                            _fh.write(_marks_soups[0].prettify())
                    except OSError:
                        pass
            except Exception as _mexc:
                print(f"[Results] marks-merge skipped: {_mexc}")

        print(
            f"[Results] tables={len(candidate_tables)} sem={sem_id} "
            f"subjects={len(subject_grades)} sgpa_rows={len(parsed_results)} "
            f"sgpa={active_sgpa_text or '-'} cgpa={active_cgpa_text or '-'} "
            f"pending={1 if semester_pending else 0} "
            f"(debug: results_debug.html)"
        )

        return {
            "success": True,
            "results": parsed_results,
            "global_cgpa": global_cgpa,
            "subject_grades": subject_grades,
            "available_sems": available_sems,
            "active_sem": sem_id,
            # ⭐ Ongoing semester ka result page pe nahi (declare nahi hua)
            "semester_pending": semester_pending,
            # ⭐ Page pe visible ACTIVE semester ka SGPA/CGPA (postback ke
            # baad ye values us semester ki hoti hain) - views ko history-
            # regex ke bina direct card dikhaane deta hai.
            "active_sgpa": active_sgpa_text,
            "active_cgpa": active_cgpa_text,
        }

    # ------------------------------------------------------------------
    # ⭐ FEES SCRAPER
    # ------------------------------------------------------------------
    def _caller_all_paid_overview(self, records, summary):
        # ⭐ v5.18 CALLER ALL-PAID GUARD (user console 02-Aug-2026 FINAL):
        # FINAL artifacts pe decide karo (parse-level mix se azaad):
        #   saare records zero-money receipts (amount/paid/due sab 0)
        #   + paid unknown (0)
        #   + payable widget absent (last_pay=0)
        #   + phir bhi summary me total/due non-zero
        # => ye page-text ka narrative hai (statement/fee-structure
        #    summary), DEMAND nahi. All-paid page pe fake REMAINING DUE
        #    banned -> totals zero, views ka receipts-overview mode.
        # True return = guard fired (summary badli), False = kuch nahi kiya.
        last_pay_v = self._money_value(summary.get("last_pay"))
        paid_v = self._money_value(summary.get("paid"))
        total_v = self._money_value(summary.get("total"))
        due_v = self._money_value(summary.get("due"))
        receipts_all_zero = bool(records) and all(
            self._money_value(item.get("amount")) <= 0
            and self._money_value(item.get("paid")) <= 0
            and self._money_value(item.get("due")) <= 0
            for item in (records or [])
        )
        # ⭐ v5.20: parse ne demand ka PROOF diya hai (mini-table sum ya
        # strong labeled due) to narrative-drop guard NAHI chalega -
        # warna asli due zero ho jaati (user bug: due thi, FULLY PAID).
        if str(summary.get("_due_proven") or "").strip() == "1":
            return False
        if not (
            receipts_all_zero
            and not paid_v
            and not last_pay_v
            and (total_v or due_v)
        ):
            return False
        print(
            f"[Fees] caller ALL-PAID overview: {len(records)} zero-money "
            f"receipts + paid=0 + widget absent -> narrative "
            f"total={total_v:,.0f} / due={due_v:,.0f} DROPPED "
            "(statement ki summary hai, demand nahi)"
        )
        summary["total"] = "0"
        summary["due"] = "0"
        # ⭐ v5.20: yahan all_paid FORCE KARNA BAND (v5.19 ka bug) -
        # "widget absent" all-paid ka PROOF nahi tha (user bug: asli due
        # thi phir bhi FULLY PAID aa gaya). all_paid ab sirf parse-level
        # POSITIVE proof (blank payment-amount cells) se aata hai; yahan
        # sirf fake narrative totals drop hote hain -> UNKNOWN overview.
        return True

    def _finalize_all_paid(self, parsed_pages, records, summary):
        # ⭐ v5.22-paidscale GLOBAL ALL-PAID RULE (live console facts):
        #   Ayan (due 27,200) -> page 3 pay-now widget row ne _due_proven
        #     de diya -> demand confirmed.
        #   Adarsh (sab paid, user-confirmed 0 due) -> KISI bhi fee page
        #     pe demand-proof nahi + receipts hain + final summary ke sab
        #     money figures zero (narrative caller-guard ne drop kar di).
        # Is rule se per-page "blank payment cells" proof ki zaroorat
        # hi nahi (wo BOTH account types pe milta tha - unreliable tha).
        # Return: "1" all-paid confirm hua, "" nahi hua.
        if not records:
            return ""
        demand_page = ""
        for item in parsed_pages:
            if str(item["summary"].get("_due_proven") or "").strip() == "1":
                demand_page = item["url"]
                break
        if demand_page:
            print(f"[Fees] all-paid NO - demand proof on {demand_page}")
            return ""
        money_left = (
            self._money_value(summary.get("total"))
            or self._money_value(summary.get("paid"))
            or self._money_value(summary.get("due"))
            or self._money_value(summary.get("last_pay"))
        )
        if money_left:
            return ""
        summary["all_paid"] = "1"
        print(
            f"[Fees] ALL-PAID confirmed: {len(parsed_pages)} fee pages, "
            "kisi pe demand-proof nahi + sab money zero + receipts hain "
            "-> FULLY PAID + 100% white scale"
        )
        return "1"

    def scrape_fee_records(self, cookies_dict=None):
        """Fetch the fee summary + payment/receipt history.

        Returns:
            {"success": True,  "summary": {...}, "records": [...]}
            {"success": False, "error": "...", "summary": {}, "records": []}

        summary keys: total / paid / due (plain numeric strings)
        record keys : semester, title, amount, paid, due, date, status,
                      receipt
        """
        if cookies_dict:
            self.session.cookies = requests.utils.cookiejar_from_dict(cookies_dict)

        candidate_paths = [
            # Accounts module (this portal uses the frmAccounts* prefix -
            # confirmed via frmAccountsStudentReceiptList.aspx)
            "frmAccountsStudentReceiptList.aspx",
            "frmAccountsStudentOnlinePayment.aspx",
            "frmAccountsStudentPayDetail.aspx",
            "frmAccountsStudentLedger.aspx",
            "frmAccountsStudentLedgerReport.aspx",
            "frmAccountsStudentFeeDetail.aspx",
            "frmAccountsStudentDue.aspx",
            "frmAccountsStudentDueDetail.aspx",
            "frmAccountsStudentDues.aspx",
            "frmAccountsFeeStructure.aspx",
            "frmAccountsStudentFeeCard.aspx",
            # Generic UIMS fallbacks
            "frmFeeDetails.aspx",
            "frmStudentFee.aspx",
            "frmFeePayment.aspx",
            "frmFees.aspx",
            "FeeDetails.aspx",
            "FeePayment.aspx",
            "frmFeeLedger.aspx",
            "frmStudentFeeDetail.aspx",
            "frmFeeReceipt.aspx",
            "frmFeeRecipt.aspx",
            "FeeReceipt.aspx",
            "frmOnlineFeePayment.aspx",
            "frmStudentLedger.aspx",
            "frmLedger.aspx",
            "frmStudentAccount.aspx",
            "frmAccountDetail.aspx",
            "frmFeeStructure.aspx",
        ]

        fee_words = (
            "fee", "payment", "dues", "finance",
            "receipt", "ledger", "account",
        )

        discovered_urls = []
        postback_targets = []
        debug_lines = []

        # Step 1: scan authenticated landing pages for ANY link mentioning
        # fee/payment words - both normal hrefs AND ASP.NET __doPostBack
        # menu links (UIMS side menus often navigate via postbacks, which
        # a plain link scan completely misses).
        for landing_path in (
            # NOTE: "" / default.aspx / home.aspx resolve to the LOGIN
            # page ROOT. Hitting it while authenticated makes UIMS reset
            # the session cookie - that poisoned every later request
            # ("session expired" errors on semester switching).
            "frmStudentHome.aspx",
            "frmMyTimeTable.aspx",
            "frmStudentMarksView.aspx",
            "frmStudentCourseWiseAttendanceSummary.aspx",
        ):
            landing_url = urljoin(self.auth_url, landing_path)
            try:
                landing_response = self.session.get(landing_url, timeout=10)
                if (
                    landing_response.status_code != 200
                    or "login" in landing_response.url.lower()
                ):
                    continue
            except requests.RequestException:
                continue

            landing_soup = BeautifulSoup(landing_response.text, "html.parser")
            for anchor in landing_soup.find_all("a", href=True):
                label = anchor.get_text(" ", strip=True).lower()
                href = anchor.get("href", "").strip()
                if not any(word in label or word in href.lower() for word in fee_words):
                    continue

                debug_lines.append(f"LINK [{landing_path}] {label!r} -> {href}")

                postback = re.match(
                    r"javascript:__doPostBack\('([^']+)'\s*,\s*'([^']*)'\)",
                    href,
                    flags=re.I,
                )
                if postback:
                    postback_targets.append(
                        (landing_url, postback.group(1), postback.group(2), label)
                    )
                elif href and not href.startswith("#") and not href.lower().startswith("javascript:"):
                    discovered_urls.append(urljoin(landing_url, href))

        candidate_urls = discovered_urls + [
            urljoin(self.auth_url, path) for path in candidate_paths
        ]
        # ⭐ v5.20: due-ish pages ko PRIORITY - receipt-list pages pe DUE
        # hoti hi nahi par wo fetch-cap kha jaati thi (isliye asli due
        # wala page kabhi fetch hi nahi hua -> fake FULLY PAID).
        _dueish_words = (
            "due", "ledger", "payable", "onlinepayment",
            "paydetail", "installment", "instalment",
        )

        def _fee_url_rank(item_url):
            low = item_url.lower()
            if any(word in low for word in _dueish_words):
                return 0
            if "receipt" in low:
                return 1
            return 2

        candidate_urls = sorted(candidate_urls, key=_fee_url_rank)
        # ⭐ v5.21: duplicate URLs hatao (receipt-list 2 baar fetch ho
        # rahi thi - wasted request + double parse).
        candidate_urls = list(dict.fromkeys(candidate_urls))

        # Step 2: plain GET on every candidate. Collect ALL fee-looking
        # pages - a receipts-only page makes TOTAL == PAID and DUE == 0,
        # which is usually wrong. We need the due/ledger page too.
        fee_pages = []
        for page_url in candidate_urls:
            try:
                response = self.session.get(page_url, timeout=10)
            except requests.RequestException as exc:
                debug_lines.append(f"GET {page_url} -> ERROR {exc}")
                continue
            debug_lines.append(f"GET {page_url} -> {response.status_code} {response.url}")
            if "login" in response.url.lower():
                # Session is dead - every further guessed request only
                # risks more damage. Stop immediately.
                debug_lines.append("PORTAL SESSION DIED -> aborting fee discovery")
                break
            if response.status_code != 200:
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            if self._looks_like_fee_page(soup):
                fee_pages.append((soup, response.url))
            if len(fee_pages) >= 5:
                break

        # Step 3: follow ASP.NET postback menu links (UIMS side menu).
        # ⭐ v5.20: fee pages mil bhi gaye hon par unme DEMAND ka koi
        # signal nahi (payment-amount cells blank / payable widget absent)
        # to bhi "dues/payment" postback links follow karo - UIMS me asli
        # due aksar side-menu "Dues" page pe hoti hai.
        def _fee_pages_show_demand(pages):
            for sp, _purl in pages:
                blob = sp.get_text(" ", strip=True)
                if re.search(
                    r"payment\s*amount\s*[:\-]?\s*(?:₹|rs\.?|inr)?\s*[0-9]",
                    blob, flags=re.I,
                ):
                    return True
                if re.search(
                    r"(?:dues?|balance|outstanding|pending|payable)"
                    r"(?:\s*(?:fee|amount))?\s*[:\-]\s*(?:₹|rs\.?|inr)?\s*[0-9]",
                    blob, flags=re.I,
                ):
                    return True
                for _table in sp.find_all("table"):
                    _rows = _table.find_all("tr")
                    if not _rows:
                        continue
                    _hdr = " ".join(
                        cell.get_text(" ", strip=True).lower()
                        for cell in _rows[0].find_all(["th", "td"])
                    )
                    if "payable" in _hdr:
                        return True
            return False

        need_due_page = not fee_pages or not _fee_pages_show_demand(fee_pages)
        if need_due_page:
            postback_found = 0
            for page_url, target, argument, label in postback_targets:
                if fee_pages and not any(
                    word in (label + " " + target).lower()
                    for word in ("due", "fee", "payment", "payable", "account")
                ):
                    continue
                try:
                    base_response = self.session.get(page_url, timeout=10)
                    base_soup = BeautifulSoup(base_response.text, "html.parser")
                    data = self._hidden_fields(base_soup)
                    data["__EVENTTARGET"] = target
                    data["__EVENTARGUMENT"] = argument
                    response = self.session.post(
                        page_url,
                        data=data,
                        cookies=self.session.cookies,
                        timeout=10,
                        allow_redirects=True,
                    )
                except requests.RequestException as exc:
                    debug_lines.append(f"POST {page_url} target={target} -> ERROR {exc}")
                    continue
                debug_lines.append(
                    f"POST {page_url} target={target} "
                    f"-> {response.status_code} {response.url}"
                )
                if "login" in response.url.lower():
                    debug_lines.append("PORTAL SESSION DIED -> aborting fee postbacks")
                    break
                soup = BeautifulSoup(response.text, "html.parser")
                if self._looks_like_fee_page(soup):
                    fee_pages.append((soup, response.url))
                    postback_found += 1
                if postback_found >= 2:
                    break

        try:
            with open("fees_debug.txt", "w", encoding="utf-8") as debug_file:
                debug_file.write("\n".join(debug_lines))
        except OSError:
            pass

        if not fee_pages:
            print("[Fees] Fee page was not found on the portal (see fees_debug.txt).")
            return {
                "success": False,
                "error": "Fee page was not found on the portal.",
                "summary": {},
                "records": [],
            }

        # Parse every fee page, then mix the best sources:
        #  - RECORDS  <- the page with the most payment rows
        #  - SUMMARY  <- the page that actually talks about due/balance
        parsed_pages = []
        for soup_page, page_url in fee_pages:
            try:
                records, summary = self._parse_fee_page(soup_page, page_url)
            except Exception as exc:
                print(f"[Fees] Parse error on {page_url}: {exc}")
                continue
            text_lower = soup_page.get_text(" ", strip=True).lower()
            parsed_pages.append({
                "records": records,
                "summary": summary,
                "url": page_url,
                "soup": soup_page,
                "has_due_words": any(word in text_lower for word in (
                    "due", "balance", "outstanding",
                    "payable", "installment", "instalment",
                )),
            })

        if not parsed_pages:
            return {
                "success": False,
                "error": "Fee parsing failed.",
                "summary": {},
                "records": [],
            }

        # ⭐ v5.20 DEBUG: har fetched fee page dump + signal line - ab
        # sirf console paste karne se exact portal structure mil jayegi.
        for idx, item in enumerate(parsed_pages, 1):
            # ⭐ Portal (culko) har page pe one-time password PLAIN TEXT
            # me dikhata hai - debug file/console me leak na ho, mask.
            def _pw_mask(blob):
                return re.sub(
                    r"(?i)(password\s*[:\-]?\s*)\S+", r"\1***", blob,
                )
            try:
                with open(f"fees_page_{idx}.html", "w", encoding="utf-8") as fh:
                    fh.write(_pw_mask(str(item["soup"])))
            except OSError:
                pass
            _ptext = _pw_mask(item["soup"].get_text(" ", strip=True))
            _cells = re.findall(
                r"payment\s*amount\s*[:\-]?\s*(?:₹|rs\.?|inr)?\s*"
                r"([0-9][0-9,]*(?:\.\d{1,2})?)",
                _ptext, flags=re.I,
            )
            print(
                f"[Fees Page {idx}] {item['url']} rec={len(item['records'])} "
                f"summary={item['summary']} paycells={_cells[:6]}"
            )
            _money_ctx = []
            for _m in re.finditer(
                r"(?:₹|rs\.?|inr)?\s*[0-9][0-9,]{2,}(?:\.\d{1,2})?", _ptext
            ):
                _ctx = _ptext[max(0, _m.start() - 40): _m.end() + 10]
                _ctx = re.sub(r"\s+", " ", _ctx).strip()
                if _ctx not in _money_ctx:
                    _money_ctx.append(_ctx)
                if len(_money_ctx) >= 20:
                    break
            print(f"[Fees Page {idx} money-ctx] " + " | ".join(_money_ctx))

        records_page = max(parsed_pages, key=lambda item: len(item["records"]))
        due_pages = [
            item for item in parsed_pages
            if item["has_due_words"] and (
                self._money_value(item["summary"].get("due")) > 0
                or self._money_value(item["summary"].get("total")) > 0
            )
        ]
        summary_source = due_pages[0] if due_pages else records_page

        records = records_page["records"]
        summary = dict(summary_source["summary"])
        used_url = summary_source["url"]

        # ⭐ Har receipt ka Download link bhejo - Django view baad me isi
        # link se official PDF download karke dega. Amount scrape karna
        # BAND: receipt PDFs ke andar embedded font data junk digits banata
        # hai (isliye 472,646 jaisa fake amount aa raha tha).
        receipts_map = {}
        for record in records:
            link = record.pop("_link", "")
            receipt_no = (record.get("receipt") or "").strip()
            if link and receipt_no and receipt_no not in receipts_map:
                receipts_map[receipt_no] = {
                    "page_url": records_page["url"],
                    "link": link,
                }

        # Console me pehli 5 parsed rows dikhao - links verify karne ke liye
        for sample in records[:5]:
            print(f"[Fees Row] {sample}")

        total_v = self._money_value(summary.get("total"))
        paid_v = self._money_value(summary.get("paid"))
        due_v = self._money_value(summary.get("due"))
        if total_v and not due_v:
            summary["due"] = f"{max(0.0, total_v - paid_v):,.0f}"
        # ⭐ v5.10: purana `summary["total"] = summary["paid"]` fallback
        # DELETE (wahi fake CLEAR banata tha). Aur receipt-LIST page ka
        # summary total≈paid ho to bhi status unknown hi rahe - page ke
        # sidebar/menu ke "dues/payment" links per-page guard ko beat kar
        # sakte hain, par receipt page pe DUE info hoti hi nahi.
        if (
            "receipt" in used_url.lower()
            and total_v
            and paid_v
            and not due_v
            and abs(total_v - paid_v) < 1
        ):
            print(
                "[Fees] receipt-list summary (total==paid) - status "
                "unknown, money hero hidden"
            )
            summary["total"] = ""
        # ⭐ v5.11 SANITY: paid > total = adhoora/galat "total" match hua
        # (installment/partial total - user report: portal 27,200 vs
        # scraped 15,000 -> CLEAR + 181.3% fake meter). Due mili ho to
        # liability = paid + due; warna total khali (status UNKNOWN).
        total_v = self._money_value(summary.get("total"))
        paid_v = self._money_value(summary.get("paid"))
        due_v = self._money_value(summary.get("due"))
        if total_v and paid_v > total_v:
            if due_v:
                summary["total"] = f"{paid_v + due_v:,.0f}"
                print(
                    f"[Fees] sanity total<paid - liability=paid+due={summary['total']}"
                )
            else:
                print(
                    f"[Fees] sanity total({total_v:,.0f})<paid({paid_v:,.0f}), "
                    "due unknown - money hero hidden"
                )
                summary["total"] = ""

        # ⭐ v5.18: caller-level final safety - parse-level guard ke baad
        # bhi agar narrative numbers bache hain to yahan drop honge.
        if self._caller_all_paid_overview(records, summary):
            total_v = self._money_value(summary.get("total"))
            due_v = self._money_value(summary.get("due"))
        summary.pop("_due_proven", None)
        self._finalize_all_paid(parsed_pages, records, summary)

        try:
            with open("fees_page_debug.html", "w", encoding="utf-8") as debug_file:
                debug_file.write(re.sub(
                    r"(?i)(password\s*[:\-]?\s*)\S+", r"\1***",
                    str(summary_source["soup"]),
                ))
        except OSError:
            pass

        print(
            f"[Fees] summary-url={used_url}, records={len(records)}, "
            f"download-links={len(receipts_map)}, summary={summary}"
        )
        return {
            "success": True,
            "summary": summary,
            "records": records,
            "receipts_map": receipts_map,
            "url": used_url,
        }

    @staticmethod
    def _looks_like_fee_page(soup):
        text = soup.get_text(" ", strip=True)
        lower_text = text.lower()

        if any(token in lower_text for token in (
            "page not found",
            "object reference not set",
            "server error",
            "whoops",
        )):
            return False

        has_fee_word = any(word in lower_text for word in (
            "fee", "receipt", "payment", "due",
            "balance", "ledger", "installment", "instalment",
        ))
        has_money = bool(
            re.search(r"(₹|rs\.?|inr)\s*[0-9]", text, flags=re.I)
            or re.search(r"\b\d{1,2},\d{2},\d{3}\b", text)
            or re.search(r"\b\d+,\d{3}\b", text)
        )
        return has_fee_word and (has_money or bool(soup.find_all("table")))

    def fetch_receipt_document(self, cookies_dict=None, page_url=None, link=None):
        """Download one fee receipt exactly as the portal serves it.

        ⭐ Amount parse KARNA BAND - receipt ko official PDF ki tarah hi
        download karo. Portal receipt list page se saved link (plain URL
        ya ASP.NET __doPostBack) do, aur PDF bytes wapas pao. Bytes ko
        parse nahi karte - wahi PDF user ko milta hai.

        Returns {"content": bytes, "content_type": str} ya None.
        """
        if cookies_dict:
            self.session.cookies = requests.utils.cookiejar_from_dict(cookies_dict)
        if not page_url or not link:
            return None

        postback = re.match(
            r"javascript:__doPostBack\('([^']+)'\s*,\s*'([^']*)'\)",
            link,
            flags=re.I,
        )

        try:
            if postback:
                # Postback ke liye list page ka FRESH viewstate chahiye.
                base_response = self.session.get(page_url, timeout=15)
                if "login" in base_response.url.lower():
                    return None
                base_soup = BeautifulSoup(base_response.text, "html.parser")
                data = self._hidden_fields(base_soup)
                data["__EVENTTARGET"] = postback.group(1)
                data["__EVENTARGUMENT"] = postback.group(2)
                response = self.session.post(
                    page_url,
                    data=data,
                    cookies=self.session.cookies,
                    timeout=20,
                    allow_redirects=True,
                )
            elif link.lower().startswith("javascript:") or link.startswith("#"):
                return None
            else:
                response = self.session.get(
                    urljoin(page_url, link),
                    timeout=20,
                    allow_redirects=True,
                )
        except requests.RequestException:
            return None

        if response.status_code != 200:
            return None

        content_type = response.headers.get("Content-Type", "").lower()
        is_pdf = response.content[:5] == b"%PDF-" or "pdf" in content_type

        if not is_pdf or "login" in response.url.lower():
            # HTML wapas mila matlab session expire ya galat link -
            # kuch bhi serve mat karo.
            return None

        return {"content": response.content, "content_type": "application/pdf"}

    @staticmethod
    def _money_value(text):
        match = re.search(r"([0-9][0-9,]*(?:\.\d{1,2})?)", str(text))
        if not match:
            return 0.0
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            return 0.0

    def _parse_fee_page(self, soup, page_url=None):
        page_text = soup.get_text(" ", strip=True)

        def find_amount(patterns):
            for pattern in patterns:
                match = re.search(pattern, page_text, flags=re.I)
                if match:
                    return self._money_value(match.group(1))
            return 0.0

        total = find_amount([
            # ⭐ v5.10: "Total Fee Amount: X" (Total + Fee + Amount) bhi
            # pakdo - purana pattern sirf "Total Fee:" tak limited tha.
            r"total(?:\s*(?:fee|amount)){0,2}[:\s]*(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
            r"(?:₹|rs\.?|inr)\s*([0-9][0-9,]*(?:\.\d{1,2})?)\s*total",
        ])
        paid = find_amount([
            r"(?:paid|received|deposited)(?:\s*amount)?[:\s]*(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
            r"(?:amount|fee)\s*(?:paid|received|deposited)[:\s]*(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
            # ⭐ v5.16: "Payment Amount: X" pattern PAID se HATA DIYA -
            # user hard fact: ye portal ki DEMAND (remaining due) hai,
            # jama hua paid nahi. Neeche payment-amount totals -> DUE.
        ])
        # ⭐ v5.20 STRONG LABELED DUE: "Due Fee: 27,200" / "Fee Due" /
        # "Total Due Amount" / "Balance Due" / "Outstanding" jaise LABEL
        # ke saath amount. Bare "Total 65,000" jaise ambiguous text par
        # trust nahi (wo last-payment bhi ho sakta hai). Amount ya to
        # Indian comma-format (27,200) ya 4+ plain digits ho - warna
        # session "2026" jaise numbers due ban jaate.
        _strong_due_re = (
            r"(?:total\s+|net\s+)?"
            r"(?:dues?|due\s*(?:fee|amount)|fee\s*due|balance|outstanding)"
            r"(?:\s*(?:fee|amount|dues))?"
            r"\s*[:\-]?\s*(?:₹|rs\.?|inr)?\s*"
            r"([0-9]{1,2}(?:,[0-9]{2,3})+(?:\.\d{1,2})?|[0-9]{4,}(?:\.\d{1,2})?)"
            r"(?![-/\d,.])"
        )
        due = 0.0
        strong_due_label = False
        for _sd in re.finditer(_strong_due_re, page_text, flags=re.I):
            _cand = _sd.group(1)
            _plain = _cand.replace(",", "")
            # ⭐ Academic session/year (2025/2026/2027...) due nahi hai -
            # "Fee Dues 2026" jaise heading text trap ko skip karo. Asli
            # due in rupees me hamesha is range ke bahar hoti hai.
            if (
                "," not in _cand
                and _plain.isdigit()
                and 2000 <= int(_plain) <= 2050
            ):
                continue
            due = self._money_value(_cand)
            strong_due_label = True
            print(f"[Fees] strong labeled DUE match: {_cand}")
            break
        if not due:
            due = find_amount([
                # ⭐ v5.10: "payable"/"dues" bhi DUE hai - portal ka payment
                # page "Total/Net Payable Amount" bolta hai, ye miss ho raha
                # tha (isliye kbhi due kabhi 0 nikalti thi = fake CLEAR).
                # NOTE: generic "payable" payable-widget ka last-payment bhi
                # pakad sakta hai - isliye ye SIRF fallback hai, mini-table
                # sum / strong label isko override karte hain.
                r"(?:dues?|balance|outstanding|pending|payable)(?:\s*amount)?[:\s]*(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
            ])
        # ⭐ v5.20: strong labeled due = demand ka PROOF - caller ka
        # narrative-drop guard isko zero NAHI karega.
        due_proven = bool(strong_due_label and due)

        records = []
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            header_cells = [
                cell.get_text(" ", strip=True).lower()
                for cell in rows[0].find_all(["th", "td"])
            ]
            header_blob = " ".join(header_cells)
            if not any(key in header_blob for key in (
                "receipt", "amount", "fee", "paid", "due",
                "balance", "head", "date", "install", "particular",
            )):
                continue

            # ⭐ v5.16: "particulars ... payable" wali table = pay-now
            # widget jiska TOTAL row LAST PAYMENT hai (user hard fact:
            # "15000 meri last payment") - DUE/PAID records NAHI banti.
            # Records skip; last_pay neeche wale block me parse hota hai.
            if "particular" in header_blob and "payable" in header_blob:
                print(f"[Fees Table] payable widget skipped (records) headers={header_cells}")
                continue

            print(f"[Fees Table] headers={header_cells}")

            def col(*names):
                for name in names:
                    for index, header in enumerate(header_cells):
                        if name in header:
                            return index
                return None

            idx_title = col("head", "particular", "description", "fee type", "install")
            idx_semester = col("semester", "session", "financial")
            idx_amount = col("amount", "total")
            idx_paid = col("paid", "received", "deposit")
            idx_due = col("due", "balance", "outstanding", "pending")
            idx_date = col("date")
            idx_receipt = col("receipt", "rcpt", "voucher", "challan", "trans")
            idx_status = col("status")

            for row in rows[1:]:
                cells = [
                    cell.get_text(" ", strip=True)
                    for cell in row.find_all(["td", "th"])
                ]
                if len(cells) < 2:
                    continue
                if "total" in cells[0].lower():
                    continue

                # Grab the receipt's detail/print link - receipt LIST
                # pages carry no amount; it lives on the detail page.
                row_link = ""
                anchor = row.find("a", href=True)
                if anchor:
                    row_link = anchor.get("href", "").strip()
                if not row_link:
                    trigger = row.find(attrs={"onclick": re.compile(r"__doPostBack")})
                    if trigger:
                        postback_match = re.search(
                            r"__doPostBack\('([^']+)'\s*,\s*'([^']*)'\)",
                            trigger.get("onclick", ""),
                        )
                        if postback_match:
                            row_link = (
                                "javascript:__doPostBack('"
                                + postback_match.group(1) + "','"
                                + postback_match.group(2) + "')"
                            )

                def cell_at(index):
                    return cells[index] if index is not None and index < len(cells) else ""

                money_cells = [self._money_value(value) for value in cells]

                # Figure out which cell is the REAL amount. Never trust
                # date / session / receipt columns - "21 Jul 2026" -> 21,
                # "2026-2027" -> 2026, "UIE-11776" -> 11776.
                skip_indexes = {
                    i for i in (idx_date, idx_receipt, idx_status, idx_semester)
                    if i is not None
                }
                amount_candidates = []
                for index, value in enumerate(money_cells):
                    if index in skip_indexes or value <= 0 or value > 5000000:
                        continue
                    cell_text = cells[index].strip()
                    if re.search(r"20\d{2}\s*[-/]\s*20\d{2}", cell_text):
                        continue  # academic session, e.g. "2026-2027"
                    if re.match(r"^[A-Z]{2,}-\d{3,}$", cell_text, flags=re.I):
                        continue  # receipt-like ID, e.g. "UIE-11776"
                    amount_candidates.append(value)

                if not amount_candidates and not any(money_cells):
                    continue

                first_cell = cells[0].strip()
                if re.search(r"20\d{2}\s*[-/]\s*20\d{2}", first_cell):
                    # First column is the academic session, not a title.
                    semester_from_row = first_cell
                    title = cell_at(idx_title) or "Fee Receipt"
                else:
                    semester_from_row = ""
                    title = cell_at(idx_title) or cells[0]

                amount = self._money_value(cell_at(idx_amount))
                if not amount or amount > 5000000:
                    amount = max(amount_candidates) if amount_candidates else 0.0
                paid_cell = cell_at(idx_paid)
                due_cell = cell_at(idx_due)
                paid_value = self._money_value(paid_cell) if paid_cell else 0.0
                due_value = self._money_value(due_cell) if due_cell else 0.0

                status = cell_at(idx_status).upper()
                if status not in ("PAID", "DUE", "PENDING", "PARTIAL"):
                    # ⭐ v5.14: "payable" header wali table remaining
                    # demand dikhati hai - uske rows DUE label paoenge
                    status = "DUE" if (
                        due_value > 0 or "payable" in header_blob
                    ) else "PAID"

                if not paid_cell and status == "PAID":
                    paid_value = amount
                if not due_cell and status in ("DUE", "PENDING"):
                    due_value = amount

                records.append({
                    "semester": cell_at(idx_semester) or semester_from_row or "Fee",
                    "title": title[:60],
                    "amount": f"{amount:,.0f}",
                    "paid": f"{paid_value:,.0f}",
                    "due": f"{due_value:,.0f}",
                    "date": cell_at(idx_date),
                    "status": status,
                    "receipt": cell_at(idx_receipt),
                    "_link": row_link,
                })

        # ⭐ Amount resolution BAND. Receipt ki Download link ek PDF
        # return karti hai jiska text reliably parse nahi hota (embedded
        # font data se 472,646 jaisa junk amount aa raha tha). "_link"
        # record me rehta hai - views.py ise receipts_map me daal kar
        # user ko official PDF download karwaata hai.

        # "Payment Amount: 98,260" mini-tables (one per financial year)
        # are the page's OWN totals - far more trustworthy than summing
        # receipt rows (receipt NUMBERS look like money to a parser).
        payment_amounts = [
            self._money_value(match.group(1))
            for match in re.finditer(
                r"payment\s*amount\s*[:\-]?\s*(?:₹|rs\.?|inr)?\s*"
                r"([0-9][0-9,]*(?:\.\d{1,2})?)",
                page_text,
                flags=re.I,
            )
        ]
        unique_payment_amounts = list(dict.fromkeys(payment_amounts))
        # ⭐ v5.15: in "Payment Amount: X" totals ko PAID banana BAND -
        # user hard fact: ye portal DEMAND/REMAINING dikhata hai
        # ("due 27200 hi mera"), jama hua paid nahi. due step neeche.

        # ⭐ v5.15 FINAL FEE MAPPING (user ke 2 hard facts):
        #   1) "Payment Amount: X" mini-tables ka sum = REMAINING/DUE
        #      (user: "due 27200 hi mera") - PAID nahi hai.
        #   2) payable-particulars table ka TOTAL (15,000) = LAST
        #      PAYMENT (already jama ho chuki) - due nahi, sirf info.
        last_pay = 0.0
        # ⭐ v5.21: pay-now widget ki DATA rows = CURRENT DEMAND (05-Aug
        # LIVE console fact - Ayan page: headers 'particulars / fee
        # payable / extension / late fee / payable amount / to pay fee
        # payment', row "Semester Fee Rs 27200 Rs 27200 0.00 Rs 27200" =
        # asli due 27,200). All-paid accounts pe ye widget/rows hote hi
        # nahi (widget absent = purana behavior same) - fake due risk 0.
        widget_due = 0.0
        for table in soup.find_all("table"):
            pt_rows = table.find_all("tr")
            if len(pt_rows) < 2:
                continue
            pt_headers = [
                cell.get_text(" ", strip=True).lower()
                for cell in pt_rows[0].find_all(["th", "td"])
            ]
            pt_blob = " ".join(pt_headers)
            if "payable" not in pt_blob:
                continue
            idx_payable = None
            for h_index, h_text in enumerate(pt_headers):
                if "payable" in h_text and "fee payable" not in h_text:
                    idx_payable = h_index
                    break
            if idx_payable is None:
                for h_index, h_text in enumerate(pt_headers):
                    if "payable" in h_text:
                        idx_payable = h_index
                        break
            if idx_payable is None:
                continue
            for pt_row in pt_rows[1:]:
                pt_cells = [
                    cell.get_text(" ", strip=True)
                    for cell in pt_row.find_all(["td", "th"])
                ]
                if not pt_cells:
                    continue
                value = (
                    self._money_value(pt_cells[idx_payable])
                    if idx_payable < len(pt_cells) else 0.0
                )
                if "total" in pt_cells[0].lower():
                    last_pay = value          # TOTAL row = last payment
                    break
                # ⭐ v5.21: non-TOTAL widget rows = abhi dena baki demand
                # (Semester Fee jaise open fee heads). Rows ka SUM due.
                if value > 0:
                    widget_due += value
        if last_pay:
            print(f"[Fees] payable-table TOTAL = last payment {last_pay:,.0f}")
        # "Total 15,000" wala regex bhi isi last-payment ke TOTAL row se
        # aaya tha - fee ka total nahi. drop karo warna due 27,200 vs
        # total 15,000 jaisi inconsistent jodi banegi.
        if total and last_pay and abs(total - last_pay) < 1:
            print(
                f"[Fees] regex-total({total:,.0f}) == last payment - "
                "fee-total nahi tha, drop kar diya"
            )
            total = 0.0
        # REMAINING DUE = "Payment Amount: X" totals ka sum (user-confirmed)
        # ⭐ v5.16: regex-due pe OVERRIDE - regex payable-widget ka
        # last-payment amount bhi pakad leti hai, mini-tables zyada
        # bharosemand hain.
        if unique_payment_amounts:
            pay_sum = float(sum(unique_payment_amounts))
            if due and abs(due - pay_sum) > 1:
                print(
                    f"[Fees] regex-due {due:,.0f} overridden by "
                    f"payment-amount totals {pay_sum:,.0f} "
                    "(user-confirmed mapping)"
                )
            due = pay_sum
            due_proven = True
            print(
                f"[Fees] payment-amount totals = remaining DUE={due:,.0f} "
                "(user-confirmed mapping)"
            )
        # ⭐ v5.21: pay-now widget rows ka sum = current DUE (mini-table
        # blank thi to bhi ye pakad leta hai - 05-Aug live page fact).
        # Mini-table sum (v5.15 user-confirmed) ho to wahi priority me.
        if widget_due and not unique_payment_amounts:
            if due and abs(due - widget_due) > 1:
                print(
                    f"[Fees] regex-due {due:,.0f} overridden by pay-now "
                    f"widget rows {widget_due:,.0f} (05-Aug page fact)"
                )
            due = widget_due
            due_proven = True
            print(
                f"[Fees] pay-now widget rows sum = remaining DUE="
                f"{due:,.0f} (05-Aug live page fact)"
            )
        if widget_due and total < paid + due:
            total = paid + due
        # ⭐ v5.21: transaction-history "PAYMENT MODE ... Total: Rs
        # 15000.00 ... SUCCESS" = pichli successful payment - sirf
        # display hint (LAST PAYMENT subline) ke liye last_pay.
        if widget_due and not last_pay:
            _txn = re.search(
                r"total\s*:\s*rs\.?\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
                page_text, flags=re.I,
            )
            if _txn:
                last_pay = self._money_value(_txn.group(1))
                print(
                    f"[Fees] last payment from transaction history: "
                    f"{last_pay:,.0f}"
                )

        if not paid and records:
            paid = sum(self._money_value(item["paid"]) for item in records)
        if not total:
            # ⭐ v5.10: "total = paid" synthesize KARNA BAND (wahi jhootha
            # CLEAR+100% banata tha) - receipt rows ka sum hi fallback hai.
            total = sum(
                self._money_value(item["amount"]) for item in records
            )
        # ⭐ v5.17 ALL-PAID GUARD (user console truth 02-Aug-2026):
        # Statement/receipts page pe jab DEMAND ka koi STRUCTURED signal
        # hi nahi - payable widget absent (last_pay=0), "Payment Amount:"
        # cells blank (unique_payment_amounts empty), receipts ke rows me
        # koi amount nahi - tab page-text ki bare regex narrative
        # ("Total ... 65,000" / statement summary) DEMAND NAHI hoti. Ye
        # user ka jama hua paisa hai - usko DUE banana BAND (user bug:
        # sab paid hone pe bhi REMAINING 65,000 DUE dikha).
        # Dono fake paths block: (a) regex-due text-match, (b) due =
        # total-paid synthesis. total=0/due=0 -> honest receipts overview.
        # NOTE: pure payment page (records empty, "Total Payable Amount")
        # pe guard chahiye hi nahi - records hone pe hi fire hota hai.
        records_zero_money = bool(records) and all(
            self._money_value(item.get("amount")) <= 0
            and self._money_value(item.get("paid")) <= 0
            and self._money_value(item.get("due")) <= 0
            for item in records
        )
        # ⭐ v5.22: per-page "blank payment-cells = all-paid" wala proof
        # HATA DIYA - live console ne proof kar diya ki jiska DUE hai
        # (Ayan) uske receipts page pe bhi blank cells hain! Asli rule:
        # due HAI to portal kisi-na-kisi page pe DEMAND zaroor dikhata
        # hai (widget row / mini-table / strong label) -> _due_proven.
        # ALL-PAID ka final call ab CALLER-level global check karta hai
        # (_finalize_all_paid). Yahan sirf fake narrative zero hoti hai.
        if (
            records_zero_money
            and not paid
            and not unique_payment_amounts
            and not last_pay
            and (total or due)
        ):
            if strong_due_label and due:
                # ⭐ v5.20: labeled DUE asli demand hai - RAKHO (purani
                # guard yahin zero karke FULLY PAID bug banati thi).
                # Bare-narrative total unreliable -> liability=paid+due.
                print(
                    f"[Fees] strong labeled DUE={due:,.0f} asli demand - "
                    f"RAKHA; narrative total={total:,.0f} drop; "
                    "liability=paid+due"
                )
                total = paid + due
            else:
                print(
                    f"[Fees] receipts page bina demand-proof - narrative "
                    f"total={total:,.0f} due={due:,.0f} unreliable -> drop "
                    "(all-paid call caller-level _finalize_all_paid karega)"
                )
                total = 0.0
                due = 0.0
        if not due and total:
            due = max(0.0, total - paid)
        # ⭐ v5.10 FAKE-CLEAR GUARD: receipts-only page pe dikhta hua sab
        # PAID money hai - total≈paid aur page me koi due/balance/payable
        # word nahi to asli DUE yahan pata NAHI lagta. Aise page pe
        # CLEAR + full meter dikhana jhooth hai (user bug: fees pending
        # thi, phir bhi CLEAR + 100% aaya). Status unknown -> total 0;
        # views isse has_money=False karega (honest receipts overview).
        if (
            total
            and paid
            and not due
            and abs(total - paid) < 1
            and not re.search(
                r"dues?|balance|outstanding|payable|pending",
                page_text,
                flags=re.I,
            )
        ):
            print(
                "[Fees] receipts-only page (total==paid, no due words) "
                "- fee status unknown, money hero hidden"
            )
            total = 0.0

        summary = {
            "total": f"{total:,.0f}",
            "paid": f"{paid:,.0f}",
            "due": f"{due:,.0f}",
            # ⭐ v5.15: pichli jama installment (payable table ka TOTAL,
            # user-confirmed last payment) - sirf display hint.
            "last_pay": f"{last_pay:,.0f}",
            # ⭐ v5.20/5.22: demand-proof marker - caller isse (a) fake
            # narrative drop na kare, (b) GLOBAL all-paid check kare
            # (_finalize_all_paid). Return se pehle pop ho jata hai.
            "_due_proven": "1" if due_proven else "",
        }
        return records, summary
