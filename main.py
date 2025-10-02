import os
import re
import time
import json
import hashlib
import datetime as dt
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
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException


LOGIN_MENU_URL = "https://parents.codmon.com/menu"
HOME_URL = "https://parents.codmon.com/home"
IMG_HOST_PREFIX = "https://image.codmon.com/"  # 安全フィルタ用

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
            fill_result = driver.execute_script("""
                // メールアドレス入力欄（type="text" で autocomplete="email"）
                const emailInput = document.querySelector('.loginMain input[type="text"][autocomplete="email"]');
                // パスワード入力欄（type="password" で autocomplete="current-password"）
                const passInput = document.querySelector('.loginMain input[type="password"][autocomplete="current-password"]');

                console.log('Email input found:', !!emailInput);
                console.log('Password input found:', !!passInput);

                if (emailInput && passInput) {
                    emailInput.value = arguments[0];
                    emailInput.dispatchEvent(new Event('input', { bubbles: true }));
                    emailInput.dispatchEvent(new Event('change', { bubbles: true }));

                    passInput.value = arguments[1];
                    passInput.dispatchEvent(new Event('input', { bubbles: true }));
                    passInput.dispatchEvent(new Event('change', { bubbles: true }));

                    return {success: true, emailFound: true, passFound: true};
                }

                return {success: false, emailFound: !!emailInput, passFound: !!passInput};
            """, email, password)

            print(f"DEBUG: JavaScript入力結果: {fill_result}")

            if fill_result['success']:
                form_found = True
                print("DEBUG: JavaScriptでフォーム入力成功")
                time.sleep(1)

                # 送信ボタンを探してクリック
                # HTMLから判明: .loginMain__submit クラスの ons-button
                submit_result = driver.execute_script("""
                    const submitBtn = document.querySelector('.loginMain__submit');

                    console.log('Submit button found:', !!submitBtn);

                    if (submitBtn) {
                        submitBtn.click();
                        return {success: true, selector: '.loginMain__submit'};
                    }

                    return {success: false};
                """)
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

def human_time_to_dt(text: str) -> dt.datetime | None:
    """
    人間可読な時刻表記をdatetimeに変換
    対応形式:
    - "20時間前", "3日前", "10分前"
    - "9月29日"
    - "2025年9月30日16時10分31秒"
    """
    now = dt.datetime.now()

    # パターン1: "◯時間前", "◯日前", "◯分前"
    m = re.match(r"(\d+)\s*(分|時間|日)前", text)
    if m:
        val = int(m.group(1))
        unit = m.group(2)
        if unit == "分":
            return now - dt.timedelta(minutes=val)
        if unit == "時間":
            return now - dt.timedelta(hours=val)
        if unit == "日":
            return now - dt.timedelta(days=val)

    # パターン2: "9月29日" または "9月9日"
    # 時刻情報がないので、現在時刻と同じ時刻と仮定
    m = re.match(r"(\d+)月(\d+)日", text)
    if m:
        month = int(m.group(1))
        day = int(m.group(2))
        year = now.year
        # 未来の日付になる場合は前年とする
        target_date = dt.datetime(year, month, day, now.hour, now.minute, now.second)
        if target_date > now:
            target_date = dt.datetime(year - 1, month, day, now.hour, now.minute, now.second)
        return target_date

    # パターン3: "2025年9月30日16時10分31秒"
    m = re.match(r"(\d{4})年(\d+)月(\d+)日(\d+)時(\d+)分(\d+)秒", text)
    if m:
        year = int(m.group(1))
        month = int(m.group(2))
        day = int(m.group(3))
        hour = int(m.group(4))
        minute = int(m.group(5))
        second = int(m.group(6))
        return dt.datetime(year, month, day, hour, minute, second)

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

def collect_image_urls_from_home(driver, lookback_hours: int, scroll_steps: int, scroll_wait: float) -> list[tuple[str, dt.datetime|None, str|None]]:
    driver.get(HOME_URL)
    time.sleep(3)

    end_threshold = dt.datetime.now() - dt.timedelta(hours=lookback_hours)
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
        # カードはクリック可能な要素で、.homeCard_date を持つ
        card_elements = driver.find_elements(By.CSS_SELECTOR, "div.homeCard_date")

        print(f"DEBUG: 見つかったカード数: {len(card_elements)}")

        # 現在の画面に閾値を超えるカードがいくつあるかカウント
        over_threshold_count = 0

        # ホーム画面のカード全体を取得（homeCardクラス）
        home_card_elements = driver.find_elements(By.CSS_SELECTOR, "div.homeCard")
        print(f"DEBUG: 見つかったホームカード数: {len(home_card_elements)}")

        for home_card in home_card_elements:
            try:
                # カード内の日付要素を探す
                try:
                    date_el = home_card.find_element(By.CSS_SELECTOR, "div.homeCard_date span")
                    date_text = date_el.text.strip()
                except:
                    print("DEBUG: 日付要素が見つかりません、スキップ")
                    continue

                post_dt = human_time_to_dt(date_text)
                print(f"DEBUG: カード日時: {date_text} -> {post_dt}")

                # カードの一意識別子を取得（重複処理防止）
                # タイトルと日付を組み合わせて一意のIDとする
                try:
                    title_el = home_card.find_element(By.CSS_SELECTOR, "div.homeCard__title")
                    card_id = f"{date_text}_{title_el.text[:50]}"
                except:
                    card_id = date_text

                if card_id in processed_cards:
                    print(f"DEBUG: 処理済みカードをスキップ: {card_id[:50]}")
                    continue

                # LOOKBACK_HOURSの閾値チェック
                if post_dt and post_dt < end_threshold:
                    print(f"DEBUG: 閾値を超えたのでスキップ: {date_text}")
                    processed_cards.add(card_id)
                    over_threshold_count += 1
                    continue

                # この時点で閾値内の投稿のみ処理
                processed_cards.add(card_id)
                print(f"DEBUG: 処理対象のカード: {date_text} - {card_id[:50]}")

                # カードをクリックして詳細画面へ
                print("DEBUG: カードをクリック中...")
                driver.execute_script("arguments[0].scrollIntoView(true);", home_card)
                time.sleep(0.5)
                home_card.click()
                time.sleep(3)  # 詳細画面の読み込みを待つ

                # 詳細画面のHTMLを取得
                detail_html = driver.execute_script("return document.documentElement.outerHTML;")
                detail_soup = BeautifulSoup(detail_html, "html.parser")

                # 詳細画面内の現在表示されている投稿の画像のみを取得
                # カルーセルのactive状態の画像のみを取得するか、全画像を取得
                # まず、詳細画面のタイトルを確認して現在の投稿を特定
                try:
                    detail_title_el = detail_soup.select_one("div.diaryDetailTitle")
                    if detail_title_el:
                        detail_title = detail_title_el.get_text(strip=True)
                        print(f"DEBUG: 詳細画面タイトル: {detail_title[:50]}")
                except:
                    pass

                # 現在の投稿に属する画像を取得
                # 1. カルーセル内の画像を取得
                detail_imgs = detail_soup.select("ons-carousel ons-carousel-item img")
                print(f"DEBUG: カルーセル内の画像数: {len(detail_imgs)}")

                # 2. notebookPreview_meal_pic内の画像も取得
                meal_imgs = detail_soup.select("div.notebookPreview_meal_pic img")
                print(f"DEBUG: 食事画像数: {len(meal_imgs)}")

                # この投稿の画像URLリスト
                post_image_urls = []
                for img in detail_imgs:
                    src = (img.get("src") or "").strip()
                    if src.startswith(IMG_HOST_PREFIX):
                        post_image_urls.append(src)

                # 食事画像も追加
                for img in meal_imgs:
                    src = (img.get("src") or "").strip()
                    if src.startswith(IMG_HOST_PREFIX):
                        post_image_urls.append(src)

                # 最初の画像URLから投稿IDを取得（diary_idを持つもの）
                diary_id = None
                for url in post_image_urls:
                    diary_match = re.search(r'/diaries/(\d+)/', url)
                    if diary_match:
                        diary_id = diary_match.group(1)
                        print(f"DEBUG: 投稿ID: {diary_id}")
                        break

                # 画像を収集（diary_idがあればそれを使用、なければNone）
                for url in post_image_urls:
                    # diary_idがある場合、/diaries/{diary_id}/を含む画像のみフィルタ
                    # /comments/のパスを持つ画像は同じカードに属するためdiary_idを関連付ける
                    if diary_id and '/diaries/' in url:
                        if f'/diaries/{diary_id}/' in url:
                            urls.append((url, post_dt, diary_id))
                            print(f"DEBUG: 画像URL取得 (ID:{diary_id}): {url[:80]}...")
                    else:
                        # commentsパスの画像、またはdiary_idがない場合
                        urls.append((url, post_dt, diary_id))
                        print(f"DEBUG: 画像URL取得 (ID:{diary_id or 'なし'}): {url[:80]}...")

                # 戻るボタンをクリック
                print("DEBUG: 戻るボタンをクリック中...")
                back_btn = driver.find_element(By.CSS_SELECTOR, "ons-back-button")
                back_btn.click()
                time.sleep(2)  # ホーム画面への戻りを待つ

            except Exception as e:
                print(f"DEBUG: カード処理エラー: {e}")
                # エラーが起きたら戻るボタンを試す
                try:
                    back_btn = driver.find_element(By.CSS_SELECTOR, "ons-back-button")
                    back_btn.click()
                    time.sleep(2)
                except:
                    pass
                continue

        # 閾値を超えたカードが多い場合（画面の半分以上）、スクロールを停止
        print(f"DEBUG: 閾値超過カード数: {over_threshold_count}/{len(card_elements)}")
        if len(card_elements) > 0 and over_threshold_count >= len(card_elements) / 2:
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
