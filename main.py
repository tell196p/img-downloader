import os
import re
import time
import json
import hashlib
import datetime as dt
import html
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from dotenv import load_dotenv
from bs4 import BeautifulSoup
import requests

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
    StaleElementReferenceException,
    ElementClickInterceptedException,
    ElementNotInteractableException,
)
from selenium.webdriver.common.action_chains import ActionChains


LOGIN_MENU_URL = "https://parents.codmon.com/menu"
HOME_URL = "https://parents.codmon.com/home"
IMG_HOST_PREFIX = "https://image.codmon.com/"  # 安全フィルタ用

_MD_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_REL_RE = re.compile(r"(\d+)\s*(分|時間)\s*前")

def group_images_by_date(detail_html: str, now_dt, fallback_date=None):
    """DetailページHTMLから画像URLを抽出し、日付ごとにグルーピングして返す。
    - このスクリプトの仕様上、Detailページは「1つのカード（＝1日）」に対応する前提。
    - そのため、日付はまず detail_html 内の表示日付を探し、見つからなければ fallback_date を使う。
    戻り値: {date: [url, ...]}
    """
    try:
        base_date = None
        # 1) detail側に表示されている日付を探す（例: "1月23日", "2026/01/23" など）
        soup = BeautifulSoup(detail_html or "", "html.parser")
        text_candidates = []
        for sel in [
            ".diaryHeader__date", ".diary__date", ".diaryDate", ".detail__date",
            ".homeCard_date", "div.homeCard_date", "span.homeCard_date",
            ".diaryShow__date", ".diaryShowHeader__date"
        ]:
            for el in soup.select(sel):
                t = (el.get_text(" ", strip=True) or "").replace("\u3000", " ")
                if t:
                    text_candidates.append(t)

        # 画面に日付が無い場合、タイトルや本文先頭に含まれることもあるので広めに拾う
        if not text_candidates:
            # head/title
            if soup.title and soup.title.string:
                text_candidates.append(soup.title.string.strip())
            # visible text (限定的に)
            body_text = soup.get_text(" ", strip=True)
            if body_text:
                text_candidates.append(body_text[:300])

        for t in text_candidates:
            # 既存の human_time_to_dt が利用できればそれを使う（"1月23日" なども処理できる前提）
            try:
                dt_ = human_time_to_dt(t)
                if dt_:
                    base_date = dt_.date()
                    break
            except Exception:
                pass

        if base_date is None:
            if fallback_date is not None:
                base_date = fallback_date
            else:
                base_date = now_dt.date() if hasattr(now_dt, "date") else dt.datetime.now().date()

        # 2) 画像URL抽出（img/src, img/data-src, a/href を対象）
        urls = []
        for img in soup.find_all("img"):
            for key in ("src", "data-src", "data-original", "data-lazy-src"):
                u = img.get(key)
                if u:
                    urls.append(u)

        for a in soup.find_all("a"):
            u = a.get("href")
            if u:
                urls.append(u)

        # 3) 正規化・フィルタ（image.codmon.com など画像配信URLのみ）
        cleaned = []
        for u in urls:
            u = html.unescape(u) if u else u
            u = (u or "").strip()
            if not u:
                continue
            # 相対URLは無視（必要ならここで join する）
            if u.startswith("//"):
                u = "https:" + u
            if not (u.startswith("http://") or u.startswith("https://")):
                continue
            # codmon 画像配信っぽいものに絞る（必要なら緩めてもOK）
            if "image.codmon.com" not in u:
                continue
            cleaned.append(normalize_img_url(u) if "normalize_img_url" in globals() else u)

        # 重複排除（順序保持）
        seen = set()
        uniq = []
        for u in cleaned:
            if u in seen:
                continue
            seen.add(u)
            uniq.append(u)

        return {base_date: uniq}
    except Exception:
        # 最悪でも落ちないように
        try:
            return {fallback_date or (now_dt.date() if hasattr(now_dt, "date") else dt.datetime.now().date()): []}
        except Exception:
            return {}



def refetch_home_cards(driver):
    """ホームのカード要素を毎回取り直す（stale対策）。"""
    selectors = [
        "div.homeCard",
        "div[class*='homeCard']",
    ]
    for sel in selectors:
        try:
            cards = driver.find_elements(By.CSS_SELECTOR, sel)
            if cards:
                return cards
        except Exception:
            pass
    return []


def safe_click(driver: webdriver.Chrome, el, timeout: float = 5.0) -> bool:
    """クリック安定化（scroll→待機→通常click→JS click→ActionChains）。"""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", el)
    except Exception:
        pass

    t_end = time.time() + timeout
    while time.time() < t_end:
        try:
            if el.is_displayed() and el.is_enabled():
                break
        except StaleElementReferenceException:
            return False
        except Exception:
            pass
        time.sleep(0.1)

    try:
        el.click()
        return True
    except (ElementClickInterceptedException, ElementNotInteractableException, WebDriverException):
        pass
    except StaleElementReferenceException:
        return False

    try:
        driver.execute_script("arguments[0].click();", el)
        return True
    except Exception:
        pass

    try:
        ActionChains(driver).move_to_element(el).pause(0.1).click(el).perform()
        return True
    except Exception:
        return False

def extract_date_text_from_card(driver, card):
    """カードから日付テキストをできるだけ頑健に取得。見つからなければNone。"""
    try:
        for css in [
            "div.homeCard_date span",
            "div.homeCard_date",
            "div[class*='homeCard_date'] span",
            "div[class*='homeCard_date']",
            "span[class*='homeCard_date']",
        ]:
            try:
                el = card.find_element(By.CSS_SELECTOR, css)
                t = (el.text or "").strip()
                if t:
                    return t
            except Exception:
                pass

        try:
            inner = driver.execute_script(
                "return arguments[0].innerText || arguments[0].textContent || '';",
                card
            ) or ""
        except Exception:
            inner = (card.text or "")

        inner = inner.strip()
        if not inner:
            return None

        m = _MD_RE.search(inner)
        if m:
            return f"{int(m.group(1))}月{int(m.group(2))}日"

        m = _REL_RE.search(inner)
        if m:
            return f"{m.group(1)}{m.group(2)}前"

        if "昨日" in inner:
            return "昨日"
        if "今日" in inner:
            return "今日"

        return None
    except StaleElementReferenceException:
        return None

def extract_date_text_from_detail(driver):
    """詳細ページから日付テキストを取得（カードから取れない時の保険）。"""
    for css in [
        "div.diaryDetail_date",
        "div.diaryDetail__date",
        "div[class*='Detail'][class*='date']",
        "div[class*='date']",
    ]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, css)
            t = (el.text or "").strip()
            if t:
                return t
        except Exception:
            pass

    try:
        body = driver.find_element(By.TAG_NAME, "body").text or ""
        m = _MD_RE.search(body)
        if m:
            return f"{int(m.group(1))}月{int(m.group(2))}日"
    except Exception:
        pass

    return None

def env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name, str(default)).strip().lower()
    return val in ("1", "true", "yes", "on")

def setup_driver(headless: bool):
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,2000")
    opts.add_argument("--lang=ja-JP")
    # JavaScript実行を確実にする
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option('useAutomationExtension', False)
    # User-Agentを設定
    opts.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
    opts.binary_location = "/usr/bin/google-chrome"
    return webdriver.Chrome(service=Service(executable_path="/usr/local/bin/chromedriver"), options=opts)

def login_and_get_cookies(email: str, password: str, headless: bool=True):
    driver = setup_driver(headless)

    # WebDriverWaitの設定
    wait = WebDriverWait(driver, 10)

    try:
        driver.get(LOGIN_MENU_URL)
        print(f"DEBUG: ページ読み込み開始: {LOGIN_MENU_URL}")

        # ページの完全な読み込みを待つ
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        print("DEBUG: ページ読み込み完了（readyState=complete）")

        # さらに少し待機（JavaScriptの実行を待つ）
        time.sleep(3)

        # デバッグ: 初期ページの状態を保存
        debug_dir = Path("/data/python/debug")
        debug_dir.mkdir(exist_ok=True)
        driver.save_screenshot(str(debug_dir / "01_initial_page.png"))

        # JavaScriptで最新のDOMを取得
        initial_html = driver.execute_script("return document.documentElement.outerHTML;")
        (debug_dir / "01_initial_page.html").write_text(initial_html, encoding="utf-8")
        print(f"DEBUG: ページタイトル: {driver.title}")
        print(f"DEBUG: 現在のURL: {driver.current_url}")
        print(f"DEBUG: HTMLサイズ: {len(initial_html)} bytes")

    except TimeoutException:
        print("DEBUG: ページ読み込みタイムアウト")
        driver.save_screenshot(str(debug_dir / "error_timeout.png"))
        driver.quit()
        raise

    # ログインフォームの推定セレクタ（サイト側変更時はここを修正）
    # 1) 直接フォームが出ている場合
    selectors = [
        {"email": (By.NAME, "email"), "pass": (By.NAME, "password"), "submit": (By.CSS_SELECTOR, "button[type=submit]")},
        {"email": (By.CSS_SELECTOR, "input[type=email]"), "pass": (By.CSS_SELECTOR, "input[type=password]"), "submit": (By.CSS_SELECTOR, "button, input[type=submit]")},
        {"email": (By.ID, "email"), "pass": (By.ID, "password"), "submit": (By.CSS_SELECTOR, "button[type=submit], input[type=submit]")},
    ]

    form_found = False
    for idx, sel in enumerate(selectors):
        try:
            print(f"DEBUG: セレクタ{idx+1}を試行中...")
            email_el = driver.find_element(*sel["email"])
            pass_el = driver.find_element(*sel["pass"])
            form_found = True
            print(f"DEBUG: セレクタ{idx+1}でフォーム検出成功")
            email_el.clear(); email_el.send_keys(email)
            pass_el.clear(); pass_el.send_keys(password)
            # 送信
            try:
                driver.find_element(*sel["submit"]).click()
            except NoSuchElementException:
                pass_el.send_keys(Keys.ENTER)
            time.sleep(3)
            driver.save_screenshot(str(debug_dir / "02_after_submit.png"))
            break
        except NoSuchElementException as e:
            print(f"DEBUG: セレクタ{idx+1}失敗: {e}")
            continue

    if not form_found:
        # メニューからログインページに誘導されるタイプの場合、ログインリンクをクリック
        print("DEBUG: フォーム直接検出失敗、ログインリンクを探索中...")
        try:
            # 「すでにアカウントをお持ちの方」のdivをクリック
            # WebDriverWaitで要素が表示されるまで待つ
            print("DEBUG: menu__loginLinkを待機中...")
            login_link = wait.until(EC.presence_of_element_located((By.CLASS_NAME, "menu__loginLink")))
            print(f"DEBUG: ログインリンク発見: {login_link.text}")

            # スクロールして要素を表示領域に持ってくる
            driver.execute_script("arguments[0].scrollIntoView(true);", login_link)
            time.sleep(0.5)

            # クリック
            login_link.click()
            print("DEBUG: ログインリンクをクリックしました")

            # フォームが表示されるまで待つ（input[type=email]またはinput[type=password]が出現するまで）
            print("DEBUG: ログインフォームの表示を待機中...")
            try:
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type=email], input[type=password], input[name=email]")))
                print("DEBUG: ログインフォームが表示されました")
            except TimeoutException:
                print("DEBUG: ログインフォーム表示のタイムアウト（10秒）")

            time.sleep(1)  # 追加の安定待機
            driver.save_screenshot(str(debug_dir / "03_after_login_click.png"))

            # JavaScriptで最新のDOMを取得
            current_html = driver.execute_script("return document.documentElement.outerHTML;")
            (debug_dir / "03_after_login_click.html").write_text(current_html, encoding="utf-8")
            print(f"DEBUG: クリック後HTMLサイズ: {len(current_html)} bytes")

            # フォームの存在確認
            has_form = driver.execute_script("""
                return document.querySelector('input[type=email], input[type=password], input[name=email]') !== null;
            """)
            print(f"DEBUG: フォーム要素の存在: {has_form}")

            # フォームが表示されたはずなので、JavaScriptで直接入力を試みる
            print("DEBUG: JavaScriptで直接フォーム入力を試行中...")

            # JavaScriptでinput要素を探して値を設定
            # HTMLから判明: .loginMain内の input[type="text"] と input[type="password"]
            fill_result = driver.execute_script("""// Deep query (supports shadow DOM)
function queryAllDeep(selector, root=document) {
  const results = [];
  const visit = (node) => {
    if (!node) return;
    try {
      if (node.querySelectorAll) {
        results.push(...node.querySelectorAll(selector));
      }
    } catch(e) {}
    // traverse shadow roots
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_ELEMENT, null);
    let cur = walker.currentNode;
    while (cur) {
      if (cur.shadowRoot) visit(cur.shadowRoot);
      cur = walker.nextNode();
    }
  };
  visit(root);
  // dedupe while preserving order
  const seen = new Set();
  return results.filter(el => {
    if (!el) return false;
    const k = el;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

function isVisible(el) {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = el.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}

function setNativeValue(el, value) {
  const proto = Object.getPrototypeOf(el);
  const desc = Object.getOwnPropertyDescriptor(proto, 'value');
  const setter = desc && desc.set ? desc.set : null;
  if (setter) setter.call(el, value);
  else el.value = value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
}

const container = document.querySelector('.loginMain') || document;
const inputs = queryAllDeep('input', container).filter(el => !el.disabled && el.type !== 'hidden' && isVisible(el));

let passInput = inputs.find(el => (el.type || '').toLowerCase() === 'password' || (el.getAttribute('autocomplete') || '').toLowerCase().includes('password'));
let emailInput = inputs.find(el => {
  const t = (el.type || '').toLowerCase();
  const name = (el.name || '').toLowerCase();
  const id = (el.id || '').toLowerCase();
  const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
  const im = (el.getAttribute('inputmode') || '').toLowerCase();
  const ph = (el.getAttribute('placeholder') || '');
  return t === 'email' || ac === 'email' || im === 'email' || name.includes('mail') || id.includes('mail') || /メール|mail/i.test(ph);
});

// Fallback: first visible non-password input
if (!emailInput) {
  emailInput = inputs.find(el => el !== passInput && (el.type || '').toLowerCase() !== 'password');
}

const result = { emailFound: !!emailInput, passFound: !!passInput, success: false };

if (emailInput && passInput) {
  emailInput.focus();
  setNativeValue(emailInput, arguments[0]);
  passInput.focus();
  setNativeValue(passInput, arguments[1]);
  result.success = true;
}

return result;""", email, password)

            print(f"DEBUG: JavaScript入力結果: {fill_result}")

            if fill_result['success']:
                form_found = True
                print("DEBUG: JavaScriptでフォーム入力成功")
                time.sleep(1)

                # 送信ボタンを探してクリック
                # HTMLから判明: .loginMain__submit クラスの ons-button
                submit_result = driver.execute_script("""const container = document.querySelector('.loginMain') || document;
const candidates = [
  '.loginMain__submit',
  'button[type="submit"]',
  'input[type="submit"]',
  'button',
  'input[type="button"]'
];

function isVisible(el){
  if (!el) return false;
  const s = window.getComputedStyle(el);
  if (s.display==='none' || s.visibility==='hidden' || s.opacity==='0') return false;
  const r = el.getBoundingClientRect();
  return r.width>0 && r.height>0;
}

let btn = null;
for (const sel of candidates){
  const els = container.querySelectorAll(sel);
  for (const el of els){
    const t = (el.innerText || el.value || '').trim();
    // Prefer explicit submit button or text containing "ログイン"
    if (sel === '.loginMain__submit') { btn = el; break; }
    if (/ログイン|Login|Sign in/i.test(t)) { btn = el; break; }
  }
  if (btn) break;
}
if (!btn){
  // last resort: first visible button
  const els = container.querySelectorAll('button, input[type="submit"], input[type="button"]');
  for (const el of els){
    if (isVisible(el) && !el.disabled){ btn = el; break; }
  }
}
if (btn){
  btn.click();
  return { selector: btn.className || btn.tagName, success: true };
}
return { selector: null, success: false };""")
                print(f"DEBUG: 送信結果: {submit_result}")
                time.sleep(3)
                driver.save_screenshot(str(debug_dir / "04_after_submit.png"))
            else:
                # デバッグ: どんなinput要素が存在するか確認
                all_inputs = driver.execute_script("""
                    const inputs = document.querySelectorAll('input');
                    return Array.from(inputs).map(inp => ({
                        type: inp.type,
                        name: inp.name,
                        id: inp.id,
                        className: inp.className,
                        visible: inp.offsetParent !== null
                    }));
                """)
                print(f"DEBUG: ページ内の全input要素: {all_inputs}")
                raise NoSuchElementException(f"ログインフォームが表示後も検出できません。Input要素: {all_inputs}")

        except Exception as e:
            print(f"DEBUG: ログイン処理失敗: {e}")
            driver.save_screenshot(str(debug_dir / "error_no_form.png"))
            error_html = driver.execute_script("return document.documentElement.outerHTML;")
            (debug_dir / "error_no_form.html").write_text(error_html, encoding="utf-8")
            driver.quit()
            raise RuntimeError(f"ログインフォームが見つかりませんでした。debug/ディレクトリのファイルを確認してください。エラー: {e}")

    # 遷移待ち
    time.sleep(2)
    # 成功判定：/home に遷移できるか（失敗なら例外）
    try:
        driver.get(HOME_URL)
        time.sleep(2)
    except TimeoutException:
        driver.quit()
        raise RuntimeError("ログイン遷移に失敗しました。")

    # Cookie を requests.Session に移す
    sess = requests.Session()
    for c in driver.get_cookies():
        # ドメインに parents.codmon.com 系のみ適用
        domain = c.get("domain", "")
        if "codmon.com" in domain:
            sess.cookies.set(c["name"], c["value"], domain=domain, path=c.get("path", "/"))
    # UA等
    sess.headers.update({"User-Agent": driver.execute_script("return navigator.userAgent;")})

    return driver, sess

def human_time_to_dt(text: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """Convert Codmon home-card time label to datetime.

    Supported inputs:
      - Relative: '21時間前', '5分前', '3日前' など
      - Absolute (no year): '1月23日' など（年は `now.year` とみなす）

    Returns None if parse fails.
    """
    if not text:
        return None

    if now is None:
        now = dt.datetime.now()

    t = text.strip()

    # Relative time patterns (Japanese)
    m = re.match(r"^(\d+)\s*分前$", t)
    if m:
        return now - dt.timedelta(minutes=int(m.group(1)))

    m = re.match(r"^(\d+)\s*時間前$", t)
    if m:
        return now - dt.timedelta(hours=int(m.group(1)))

    m = re.match(r"^(\d+)\s*日前$", t)
    if m:
        return now - dt.timedelta(days=int(m.group(1)))

    # Absolute date: 'M月D日' optionally with weekday like '(金)'
    m = re.match(r"^(\d{1,2})月(\d{1,2})日", t)
    if m:
        month = int(m.group(1))
        day = int(m.group(2))
        try:
            # Use end-of-day to avoid excluding same-day posts when only date is shown
            return dt.datetime(now.year, month, day, 23, 59, 59)
        except ValueError:
            return None

    return None
def rewrite_width(url: str, width_value: int) -> str:
    p = urlparse(url)
    q = parse_qs(p.query)
    q["width"] = [str(width_value)]
    # 既存のforceJpgなどは維持
    new_query = urlencode({k: v[0] if isinstance(v, list) and len(v)==1 else v for k, v in q.items()}, doseq=True)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, p.fragment))

def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)

def hash_name(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]

def extract_original_filename(url: str) -> str | None:
    """
    URLから元のファイル名を抽出
    例: https://.../diaries/123/IMG_4428+%281%29.JPG?Policy=... -> IMG_4428+(1).JPG
    """
    try:
        parsed = urlparse(url)
        path_parts = parsed.path.split('/')
        if path_parts:
            # 最後のパート（ファイル名）を取得
            filename = path_parts[-1]
            # URLデコード（%xx形式を元に戻す）
            from urllib.parse import unquote
            decoded = unquote(filename)
            # 空白を_に置換して安全なファイル名にする
            safe_filename = decoded.replace(' ', '_').replace('+', '_')
            return safe_filename
    except:
        pass
    return None

def download_with_fallback(sess: requests.Session, url: str, out_path: Path, max_bytes_for_width0: int=40_000_000):
    # 1) width=0
    url0 = rewrite_width(url, 0)
    r0 = sess.get(url0, timeout=30, stream=True)
    ok0 = (r0.status_code == 200 and r0.headers.get("Content-Type","").startswith("image/"))
    if ok0:
        # サイズを先に見たい場合は一旦bytes化（streamのため分割保存）
        total = 0
        with open(out_path, "wb") as f:
            for chunk in r0.iter_content(chunk_size=8192):
                if chunk:
                    total += len(chunk)
                    if total > max_bytes_for_width0:
                        # 大きすぎ → 1080 にフォールバック
                        f.close()
                        out_path.unlink(missing_ok=True)
                        break
                    f.write(chunk)
        if out_path.exists() and out_path.stat().st_size > 0:
            return "width=0"

    # 2) width=1080
    url1080 = rewrite_width(url, 1080)
    r1 = sess.get(url1080, timeout=30)
    if r1.status_code == 200 and r1.headers.get("Content-Type","").startswith("image/"):
        out_path.write_bytes(r1.content)
        return "width=1080"

    raise RuntimeError(f"ダウンロード失敗: {url}")



def safe_go_back(driver: webdriver.Chrome, wait_timeout: int = 10) -> None:
    """詳細ページからホーム（カード一覧）に戻る。

    onsen-ui の back ボタンが存在しない/非表示の場合があるため、
    (1) 画面上に存在する「戻る/閉じる」系のボタンを素早く探してクリック
    (2) ダメなら history.back()
    (3) 最後にホームカードが表示されるまで待機
    """
    # なるべく待たずに存在チェック → 表示されていればクリック
    candidates = [
        "ons-back-button",
        "button[aria-label*='戻']",
        "button[aria-label*='back']",
        "button[aria-label*='閉']",
        "button[aria-label*='close']",
        "button[class*='back']",
        "button[class*='Back']",
        "a[class*='back']",
        "a[class*='Back']",
        "div[class*='back'] button",
    ]

    clicked = False
    for sel in candidates:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in elems:
                try:
                    if el.is_displayed() and el.is_enabled():
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        driver.execute_script("arguments[0].click();", el)
                        clicked = True
                        break
                except Exception:
                    continue
            if clicked:
                break
        except Exception:
            continue

    if not clicked:
        try:
            driver.execute_script("window.history.back();")
        except Exception:
            try:
                driver.back()
            except Exception:
                pass

    # ホームに戻ったことの確認（1回だけ待つ）
    try:
        WebDriverWait(driver, wait_timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.homeCard, div[class*='homeCard']"))
        )
    except Exception:
        # ここで失敗しても致命ではない（次の refetch で復帰する可能性がある）
        pass
def collect_image_urls_from_home(driver, lookback_hours: int, scroll_steps: int, scroll_wait: float) -> list[tuple[str, dt.datetime|None, str|None]]:
    items: list[tuple[str, dt.datetime|None, str|None]] = []
    
    driver.get(HOME_URL)
    time.sleep(3)

    now = dt.datetime.now()
    end_threshold = now - dt.timedelta(hours=lookback_hours)
    print(f"DEBUG: 閾値時刻: {end_threshold.strftime('%Y-%m-%d %H:%M:%S')}")

    urls: list[tuple[str, dt.datetime|None, str|None]] = []
    processed_cards = set()  # 処理済みカードを追跡
    should_stop = False  # 閾値に達したらTrue

    last_h = 0
    for i in range(scroll_steps):
        if should_stop:
            print("DEBUG: 閾値に達したため、これ以上スクロールしません")
            break

        print(f"DEBUG: スクロール {i+1}/{scroll_steps}")

        # JavaScriptで最新のDOMを取得してBeautifulSoupで解析
        current_html = driver.execute_script("return document.documentElement.outerHTML;")
        soup = BeautifulSoup(current_html, "html.parser")

        # カード要素を取得（画像を含むカード）
        # ホーム画面のカード全体を取得（homeCardクラス）
        try:
            home_card_elements = driver.find_elements(By.CSS_SELECTOR, "div.homeCard")
        except Exception:
            home_card_elements = []
        print(f"DEBUG: 見つかったカード数: {len(home_card_elements)}")

        # 現在の画面に閾値を超えるカードがいくつあるかカウント
        over_threshold_count = 0

        print(f"DEBUG: 見つかったホームカード数: {len(home_card_elements)}")

        
        # stale対策のため、カードをindexで処理し、毎回取り直す
        base_cards = refetch_home_cards(driver)
        print(f"DEBUG: 見つかったカード数: {len(base_cards)}")

        for card_idx in range(len(base_cards)):
            try:
                cards_now = refetch_home_cards(driver)
                if card_idx >= len(cards_now):
                    break
                home_card = cards_now[card_idx]

                date_text = extract_date_text_from_card(driver, home_card)
                if not date_text:
                    print("DEBUG: 日付要素が見つかりません（カード内）、詳細から取得を試行します")

                post_dt = human_time_to_dt(date_text, now) if date_text else None
                if post_dt and post_dt < end_threshold:
                    over_threshold_count += 1

                # 詳細へ遷移（interactable/intercepted/stale 対策）
                try:
                    if not safe_click(driver, home_card, timeout=5.0):
                        print("DEBUG: カードクリックに失敗しました、スキップ")
                        continue
                except StaleElementReferenceException:
                    continue

                time.sleep(0.8)
                detail_html = driver.page_source

                if post_dt is None:
                    detail_date_text = extract_date_text_from_detail(driver)
                    if detail_date_text:
                        post_dt = human_time_to_dt(detail_date_text, now)

                if post_dt is None:
                    print("DEBUG: 日付を特定できません（カード/詳細ともに）。このカードはスキップします")
                    safe_go_back(driver)
                    time.sleep(0.6)
                    continue

                print(f"DEBUG: 処理対象の日付: {post_dt.date()} / カード: {date_text or '(detail)'}_{card_idx}_")

                groups = group_images_by_date(detail_html, now, fallback_date=post_dt.date())
                for d, ulist in groups.items():
                    if not ulist:
                        continue
                    # 日付ごとの一覧（ログ用・重複排除用）
                    items.append((d, ulist))
                    # 後段のDL処理用にフラット化
                    for u in ulist:
                        urls.append((u, dt.datetime.combine(d, dt.time(23, 59, 59)), f"{d.isoformat()}_{card_idx}"))

                safe_go_back(driver)
                time.sleep(0.8)

            except Exception as e:
                print(f"DEBUG: カード処理エラー: {e}")
                continue

        # 閾値を超えたカードが多い場合（画面の半分以上）、スクロールを停止
        print(f"DEBUG: 閾値超過カード数: {over_threshold_count}/{len(home_card_elements)}")
        if len(home_card_elements) > 0 and over_threshold_count >= len(home_card_elements) / 2:
            print("DEBUG: 画面の半分以上が閾値を超えたため、スクロールを停止します")
            should_stop = True
            continue  # 次のスクロールループで終了

        # スクロール
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(scroll_wait)

        # スクロールが終わっても高さが変わらない＝打ち止め
        h = driver.execute_script("return document.body.scrollHeight;")
        if h == last_h:
            print("DEBUG: これ以上スクロールできないため終了")
            break
        last_h = h

    # 重複排除（URLをキーに、最初に見つかった日時とdiary_idを保持）
    unique = {}
    for u, d, diary_id in urls:
        if u not in unique:
            unique[u] = (d, diary_id)
    print(f"DEBUG: 重複排除後の画像数: {len(unique)}")
    return [(u, unique[u][0], unique[u][1]) for u in unique.keys()]

def main():
    load_dotenv()
    email = os.getenv("CODMON_EMAIL")
    password = os.getenv("CODMON_PASSWORD")
    download_dir = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
    lookback_hours = int(os.getenv("LOOKBACK_HOURS", "72"))
    max_bytes_for_width0 = int(os.getenv("MAX_BYTES_FOR_WIDTH0", "40000000"))
    headless = env_bool("HEADLESS", True)
    scroll_steps = int(os.getenv("SCROLL_STEPS", "15"))
    scroll_wait = float(os.getenv("SCROLL_WAIT_SEC", "1.0"))

    if not email or not password:
        raise RuntimeError("環境変数 CODMON_EMAIL / CODMON_PASSWORD を .env に設定してください。")

    ensure_dir(download_dir)

    driver, sess = login_and_get_cookies(email, password, headless=headless)

    try:
        items = collect_image_urls_from_home(driver, lookback_hours, scroll_steps, scroll_wait)
        print(f"収集件数: {len(items)}")

        # 保存先：日付フォルダ（今日）
        today = dt.datetime.now().strftime("%Y-%m-%d")
        out_root = download_dir / today
        ensure_dir(out_root)

        # 既存のファイル名一覧を取得（重複防止）
        existing_files = set()
        for file_path in out_root.glob("*"):
            if file_path.is_file() and not file_path.name.startswith("_"):
                existing_files.add(file_path.name)
        print(f"DEBUG: 既存ファイル数: {len(existing_files)}")

        downloaded_count = 0
        skipped_count = 0

        for url, approx_dt, diary_id in items:
            if not url.startswith(IMG_HOST_PREFIX):
                continue

            # URLから元のファイル名を抽出
            original_filename = extract_original_filename(url)
            if not original_filename:
                print(f"WARN: ファイル名抽出失敗: {url[:80]}...")
                continue

            # ファイル名：<diary_id>_<original_filename>
            # diary_idがある場合はそれを使用、ない場合は時刻を使用
            if diary_id:
                final_filename = f"{diary_id}_{original_filename}"
            else:
                stamp = (approx_dt or dt.datetime.now()).strftime("%H%M%S")
                final_filename = f"{stamp}_{original_filename}"

            # 既にファイルが存在するかチェック
            if final_filename in existing_files:
                print(f"SKIP: 既存ファイル: {final_filename}")
                skipped_count += 1
                continue

            out_path = out_root / final_filename

            try:
                used = download_with_fallback(sess, url, out_path, max_bytes_for_width0=max_bytes_for_width0)
                print(f"OK [{used}] {final_filename}")
                existing_files.add(final_filename)  # 次回のチェック用に追加
                downloaded_count += 1
            except Exception as e:
                print(f"NG {url[:80]}... -> {e}")

        print(f"\n完了: ダウンロード {downloaded_count}件、スキップ {skipped_count}件")

    finally:
        driver.quit()

if __name__ == "__main__":
    main()