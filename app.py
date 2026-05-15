#!/usr/bin/env python3
"""
네이버 블로그 통합 대시보드 — 계정 관리, RSS 동기화, 방문자 확인, 노출 확인
"""
from flask import Flask, render_template_string, request, jsonify, Response
from playwright.sync_api import sync_playwright
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET
import threading, queue, uuid, time, json, re, os, asyncio

app = Flask(__name__)
jobs = {}

# 계정 데이터는 브라우저 localStorage에 저장 — 서버는 stateless

# ── RSS 파싱 ──────────────────────────────────────────────
def fetch_rss_posts(blog_id, limit=30):
    url = f"https://rss.blog.naver.com/{blog_id}.xml"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read()
        root = ET.fromstring(data)
        items = root.findall(".//item")
        posts = []
        for item in items[:limit]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            if not title:
                continue
            m = re.search(r'/(\d{5,})(?:\?|$)', link)
            log_no = m.group(1) if m else ""
            posts.append({"title": title, "link": link, "pubDate": pub, "logNo": log_no})
        return posts
    except Exception as e:
        return {"error": str(e)}

# ── 공통 JS / Helpers ─────────────────────────────────────
STEALTH_JS = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});
Object.defineProperty(navigator,'languages',{get:()=>['ko-KR','ko','en-US','en']});
window.chrome={runtime:{}};
"""

SECTION_MAP = {
    "view":"VIEW","blog":"블로그","news":"뉴스",
    "cafearticle":"카페","cafe":"카페","kin":"지식IN",
    "webkr":"웹사이트","site":"웹사이트",
    "image":"이미지","video":"동영상","shopping":"쇼핑",
    "local":"지도/플레이스","map":"지도","place":"플레이스","post":"포스트",
    "book":"책","dict":"어학사전","academ":"학술정보",
}

def map_section(raw):
    raw = (raw or "").strip().lower()
    if raw in SECTION_MAP: return SECTION_MAP[raw]
    for p in ("section_","sp_nws","sp_"):
        if raw.startswith(p):
            k = raw[len(p):]
            if k in SECTION_MAP: return SECTION_MAP[k]
    return SECTION_MAP.get(raw, raw if raw else None)

def normalize(s):
    s = re.sub(r'[\s·•・,.\-_/\\\'\"!?~\[\]()]+', ' ', s or '')
    return s.lower().strip()

def blog_id_match(href, blog_ids):
    if not href or not blog_ids: return False
    h = href.lower()
    for bid in blog_ids:
        bid = bid.lower().strip()
        if not bid: continue
        if re.search(r'blog\.naver\.com/' + re.escape(bid) + r'(/|$|\?)', h):
            return True
    return False

def title_match_item(item_text, target_title):
    if not item_text or not target_title: return False
    ni = normalize(item_text); nt = normalize(target_title)
    if nt in ni: return True
    if len(ni) >= 6 and ni in nt: return True
    key_t = nt.replace(' ',''); key_i = ni.replace(' ','')
    if len(key_t) >= 6 and key_t in key_i: return True
    if len(key_i) >= 6 and key_i in key_t: return True
    words = [w for w in nt.split() if len(w) >= 2]
    if len(words) < 2: return False
    matched = sum(1 for w in words if w in ni)
    return matched / len(words) >= 0.65

EXTRACT_JS = r"""
() => {
    const result = {};
    function addItem(type, text, href) {
        const t = (text||'').replace(/\s+/g,' ').trim();
        if (!type || t.length < 5 || t.length > 300) return;
        if (!result[type]) result[type] = [];
        const h = href || '';
        if (!result[type].some(x => x.text === t && x.href === h))
            result[type].push({text: t, href: h});
    }
    const TITLE_SELS = [
        '.total_tit','a.api_txt_lines','.title_link','.link_tit','.news_tit',
        '.tit_txt','.tit_area .tit','[class*="title_link"]','[class*="total_tit"]',
        'a[href*="blog.naver.com"]','a[href*="cafe.naver.com"]',
        'a[href*="post.naver.com"]','a[href*="m.blog.naver.com"]',
    ].join(',');
    function getSectionType(el) {
        const id=(el.id||'').toLowerCase(), cls=(el.className||'').toLowerCase();
        if (el.hasAttribute('data-type')) return el.getAttribute('data-type');
        if (id.startsWith('sp_')) return id.replace(/^sp_nws/,'news').replace(/^sp_/,'');
        if (id.startsWith('section_')) return id.replace(/^section_/,'');
        if (cls.includes('cs_blog')||cls.includes('sc_blog')) return 'blog';
        if (cls.includes('cs_news')||cls.includes('sc_news')) return 'news';
        if (cls.includes('cs_cafe')||cls.includes('sc_cafe')) return 'cafearticle';
        if (cls.includes('cs_view')||cls.includes('sc_view')) return 'view';
        if (cls.includes('cs_kin')) return 'kin';
        if (cls.includes('cs_web')||cls.includes('sc_web')) return 'webkr';
        return null;
    }
    const seen = new Set();
    document.querySelectorAll(
        '[data-type],[id^="sp_"],[id^="section_"],[class*="cs_blog"],[class*="cs_news"],' +
        '[class*="cs_cafe"],[class*="cs_view"],[class*="sc_new"],[class*="sc_blog"]'
    ).forEach(container => {
        const type = getSectionType(container);
        if (!type || seen.has(container)) return;
        container.querySelectorAll('*').forEach(c => seen.add(c));
        try { container.querySelectorAll(TITLE_SELS).forEach(el => {
            const t=(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim();
            const h=el.href||el.getAttribute('href')||'';
            if (t.length>=5 && t.length<=300) addItem(type, t, h);
        }); } catch(e) {}
    });
    const mainEl = document.querySelector('#main_pack')||document.querySelector('#ct')||
                   document.querySelector('#container')||document.body;
    if (mainEl && Object.keys(result).length === 0) {
        mainEl.querySelectorAll('a[href]').forEach(a => {
            const h=(a.href||'').toLowerCase(), t=(a.innerText||a.textContent||'').replace(/\s+/g,' ').trim();
            if (t.length<5||t.length>300) return;
            if (h.includes('blog.naver')) addItem('blog',t,a.href);
            else if (h.includes('cafe.naver')) addItem('cafearticle',t,a.href);
            else if (h.includes('post.naver')) addItem('post',t,a.href);
            else if (h.includes('kin.naver')) addItem('kin',t,a.href);
        });
    } else if (mainEl) {
        mainEl.querySelectorAll('a[href*="blog.naver"]').forEach(a => {
            const t=(a.innerText||a.textContent||'').replace(/\s+/g,' ').trim();
            if (t.length>=5&&t.length<=300) addItem('blog',t,a.href);
        });
    }
    return result;
}
"""

BLOG_EXTRACT_JS = r"""
() => {
    const links = [...document.querySelectorAll('a[href*="blog.naver.com"]')];
    return links.map(a => ({
        href: a.href || '',
        text: (a.innerText || a.textContent || '').replace(/\s+/g,' ').trim()
    })).filter(x => x.text.length > 4);
}
"""

# ── 검색 노출 확인 ────────────────────────────────────────
def find_title_in_sections(page, title, blog_ids=None):
    try: page.wait_for_load_state("networkidle", timeout=18000)
    except: pass
    try: page.wait_for_selector("#main_pack,#ct,#container,.api_subject_bx,[id^='sp_']", timeout=8000)
    except: pass
    time.sleep(2.0)
    try: section_items = page.evaluate(EXTRACT_JS)
    except Exception as e: return [], f"JS오류: {str(e)[:80]}"

    found_sections, seen = [], set()
    for raw_type, items in section_items.items():
        section_name = map_section(raw_type)
        if not section_name or section_name in seen: continue
        for item in items:
            text = item.get('text','') if isinstance(item,dict) else item
            href = item.get('href','') if isinstance(item,dict) else ''
            if blog_ids:
                if blog_id_match(href, blog_ids):
                    found_sections.append(section_name); seen.add(section_name); break
            else:
                if title_match_item(text, title):
                    found_sections.append(section_name); seen.add(section_name); break
    return found_sections, None

def crawl_keyword(keyword, title, pc_ctx, mobile_ctx, blog_ids=None):
    encoded = urllib.parse.quote(keyword)
    result = {"keyword":keyword,"title":title,"pc":[],"mobile":[],"error":None}
    try:
        page = pc_ctx.new_page()
        page.goto(f"https://search.naver.com/search.naver?query={encoded}", wait_until="networkidle", timeout=25000)
        result["pc"], err = find_title_in_sections(page, title, blog_ids)
        if err: result["error"] = err
        page.close()
    except Exception as e:
        result["error"] = str(e)[:100]
    time.sleep(0.8)
    try:
        page = mobile_ctx.new_page()
        try: page.goto(f"https://m.search.naver.com/search.naver?query={encoded}", wait_until="domcontentloaded", timeout=20000)
        except: pass
        result["mobile"], err = find_title_in_sections(page, title, blog_ids)
        if err and not result["error"]: result["error"] = err
        page.close()
    except Exception as e:
        if not result["error"]: result["error"] = str(e)[:100]
    return result

# ── 블로그 색인 확인 ──────────────────────────────────────
def check_blog_index_sync(blog_id, title, browser):
    encoded = urllib.parse.quote(f'"{title}"')
    url = f"https://search.naver.com/search.naver?where=post&query={encoded}&blogger_id={blog_id}"
    ctx = browser.new_context(
        viewport={"width":1280,"height":800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="ko-KR", extra_http_headers={"Accept-Language":"ko-KR,ko;q=0.9"},
    )
    ctx.add_init_script(STEALTH_JS)
    page = ctx.new_page()
    found = False
    try:
        try: page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except: pass
        try: page.wait_for_selector(".title_link,.api_txt_lines,[class*='title'],.cs_blog", timeout=7000)
        except: pass
        time.sleep(1.5)
        items = page.evaluate(BLOG_EXTRACT_JS)
        html_content = page.content()
        for item in items:
            if blog_id.lower() in item["href"].lower():
                nt = normalize(title); ni = normalize(item["text"])
                if nt[:20] in ni:
                    found = True; break
                words = [w for w in nt.split() if len(w) >= 2]
                if words and sum(1 for w in words if w in ni) / len(words) >= 0.7:
                    found = True; break
        if not found:
            n_html = normalize(html_content); n_title = normalize(title)
            if blog_id.lower() in n_html and n_title[:15] in n_html:
                found = True
    except: pass
    finally:
        try: page.close()
        except: pass
        try: ctx.close()
        except: pass
    return found

# ── 방문자 확인 ───────────────────────────────────────────
def check_visitor_sync(blog_id):
    """https://m.blog.naver.com/api/blogs/{blogId} — 로그인 불필요 공개 JSON API"""
    url = f"https://m.blog.naver.com/api/blogs/{blog_id}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 Chrome/124 Mobile Safari/537.36",
            "Accept": "application/json",
            "Referer": f"https://m.blog.naver.com/{blog_id}",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        result_data = data.get("result", {})
        return {
            "today": result_data.get("dayVisitorCount"),
            "total": result_data.get("totalVisitorCount"),
        }
    except Exception as e:
        return {"today": None, "error": str(e)[:100]}

# ── Job (async, 3개 동시 처리) ────────────────────────────
async def _run_job_async(posts, blog_id, kw_n, mode, q):
    from playwright.async_api import async_playwright as _ap
    sem = asyncio.Semaphore(3)

    async with _ap() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled"])

        async def process_post(i, post):
            title = post["title"]
            keyword = "".join(title.split()[:kw_n])
            pc_secs, mob_secs, indexed, err = [], [], None, None
            async with sem:
                if mode in ("exposure", "all"):
                    encoded = urllib.parse.quote(keyword)
                    for is_mobile in (False, True):
                        if is_mobile:
                            ctx = await browser.new_context(
                                viewport={"width":390,"height":844},
                                user_agent="Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36",
                                is_mobile=True, has_touch=True, locale="ko-KR",
                                extra_http_headers={"Accept-Language":"ko-KR,ko;q=0.9"},
                            )
                        else:
                            ctx = await browser.new_context(
                                viewport={"width":1920,"height":1080},
                                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                                locale="ko-KR", extra_http_headers={"Accept-Language":"ko-KR,ko;q=0.9"},
                            )
                        await ctx.add_init_script(STEALTH_JS)
                        page = await ctx.new_page()
                        try:
                            base = "https://m.search.naver.com" if is_mobile else "https://search.naver.com"
                            await page.goto(f"{base}/search.naver?query={encoded}", wait_until="domcontentloaded", timeout=22000)
                            try: await page.wait_for_selector("#main_pack,[id^='sp_'],.api_subject_bx", timeout=6000)
                            except: pass
                            await asyncio.sleep(0.6)
                            section_items = await page.evaluate(EXTRACT_JS)
                            found_secs, seen = [], set()
                            for raw_type, items in section_items.items():
                                sname = map_section(raw_type)
                                if not sname or sname in seen: continue
                                for item in items:
                                    href = item.get("href","") if isinstance(item,dict) else ""
                                    if blog_id_match(href, [blog_id]):
                                        found_secs.append(sname); seen.add(sname); break
                            if is_mobile: mob_secs = found_secs
                            else: pc_secs = found_secs
                        except Exception as e:
                            if not err: err = str(e)[:100]
                        finally:
                            try: await page.close()
                            except: pass
                            try: await ctx.close()
                            except: pass

                if mode in ("index", "all"):
                    ctx = await browser.new_context(
                        viewport={"width":1280,"height":800},
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                        locale="ko-KR", extra_http_headers={"Accept-Language":"ko-KR,ko;q=0.9"},
                    )
                    await ctx.add_init_script(STEALTH_JS)
                    page = await ctx.new_page()
                    indexed = False
                    try:
                        encoded_t = urllib.parse.quote(f'"{title}"')
                        url = f"https://search.naver.com/search.naver?where=post&query={encoded_t}&blogger_id={blog_id}"
                        try: await page.goto(url, wait_until="domcontentloaded", timeout=22000)
                        except: pass
                        try: await page.wait_for_selector(".title_link,.api_txt_lines,[class*='title'],.cs_blog", timeout=6000)
                        except: pass
                        await asyncio.sleep(0.6)
                        items = await page.evaluate(BLOG_EXTRACT_JS)
                        html_content = await page.content()
                        for item in items:
                            if blog_id.lower() in item["href"].lower():
                                nt = normalize(title); ni = normalize(item["text"])
                                if nt[:20] in ni: indexed = True; break
                                words = [w for w in nt.split() if len(w) >= 2]
                                if words and sum(1 for w in words if w in ni) / len(words) >= 0.7:
                                    indexed = True; break
                        if not indexed:
                            nh = normalize(html_content); nt2 = normalize(title)
                            if blog_id.lower() in nh and nt2[:15] in nh: indexed = True
                    except: pass
                    finally:
                        try: await page.close()
                        except: pass
                        try: await ctx.close()
                        except: pass
            return i, keyword, pc_secs, mob_secs, indexed, err

        tasks = [asyncio.create_task(process_post(i, post)) for i, post in enumerate(posts)]
        for coro in asyncio.as_completed(tasks):
            i, keyword, pc, mobile, indexed, err = await coro
            post = posts[i]
            q.put(("result", {
                "idx": i,
                "logNo": post.get("logNo",""),
                "title": post["title"],
                "link": post.get("link",""),
                "pubDate": post.get("pubDate",""),
                "keyword": keyword,
                "pc": pc,
                "mobile": mobile,
                "blog_index": indexed,
                "error": err,
                "mode": mode,
            }))
        await browser.close()


def run_job(job_id, posts, blog_id, kw_n, mode="all"):
    q = jobs[job_id]
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_job_async(posts, blog_id, kw_n, mode, q))
    except Exception as e:
        q.put(("error", str(e)))
    finally:
        loop.close()
    q.put(("done", None))

# ── API Routes (stateless — 계정 데이터는 프론트 localStorage) ──
@app.route("/api/sync", methods=["POST"])
def sync_posts():
    data = request.json or {}
    blog_id = (data.get("blogId") or "").strip().lower()
    limit = max(1, min(100, int(data.get("limit", 30))))
    if not blog_id: return jsonify({"error":"blogId 필요"}), 400
    posts = fetch_rss_posts(blog_id, limit)
    if isinstance(posts, dict) and "error" in posts:
        return jsonify({"error": posts["error"]}), 500
    return jsonify({"ok": True, "count": len(posts), "posts": posts})

@app.route("/api/visitor", methods=["POST"])
def visitor_check():
    data = request.json or {}
    blog_id = (data.get("blogId") or "").strip().lower()
    if not blog_id: return jsonify({"error":"blogId 필요"}), 400
    return jsonify(check_visitor_sync(blog_id))

@app.route("/api/check", methods=["POST"])
def start_check():
    data = request.json or {}
    blog_id = (data.get("blogId") or "").strip().lower()
    posts = data.get("posts", [])
    kw_n = max(1, min(5, int(data.get("kwN", 2))))
    mode = data.get("mode", "all")
    if mode not in ("all", "index", "exposure"):
        mode = "all"
    if not blog_id: return jsonify({"error":"blogId 필요"}), 400
    if not posts: return jsonify({"error":"게시물 없음 — RSS 동기화 먼저"}), 400
    job_id = str(uuid.uuid4())
    jobs[job_id] = queue.Queue()
    threading.Thread(target=run_job, args=(job_id, posts, blog_id, kw_n, mode), daemon=True).start()
    return jsonify({"job_id": job_id, "total": len(posts)})

@app.route("/stream/<job_id>")
def stream(job_id):
    q = jobs.get(job_id)
    if not q: return jsonify({"error":"없음"}), 404
    def gen():
        while True:
            try: t, payload = q.get(timeout=120)
            except queue.Empty:
                yield "data: [TIMEOUT]\n\n"; break
            if t == "done": yield "data: [DONE]\n\n"; break
            elif t == "error":
                yield f"data: {json.dumps({'error':payload},ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"; break
            else: yield f"data: {json.dumps(payload,ensure_ascii=False)}\n\n"
        jobs.pop(job_id, None)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no","Connection":"keep-alive"})

@app.route("/debug")
def debug():
    keyword = request.args.get("q",""); title = request.args.get("title",keyword)
    blog_ids = [b for b in request.args.get("ids","").split(",") if b.strip()]
    if not keyword: return jsonify({"error":"q 필요"}), 400
    out = {"keyword":keyword,"title":title,"blog_ids":blog_ids}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,args=["--no-sandbox","--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(viewport={"width":1920,"height":1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                locale="ko-KR")
            ctx.add_init_script(STEALTH_JS)
            page = ctx.new_page()
            page.goto(f"https://search.naver.com/search.naver?query={urllib.parse.quote(keyword)}", wait_until="networkidle", timeout=25000)
            try: page.wait_for_selector("#main_pack,[id^='sp_']", timeout=8000)
            except: pass
            time.sleep(1.5)
            section_items = page.evaluate(EXTRACT_JS)
            summary = {}
            for k, items in section_items.items():
                matched = None
                for item in items:
                    href = item.get('href','') if isinstance(item,dict) else ''
                    text = item.get('text','') if isinstance(item,dict) else item
                    if blog_ids and blog_id_match(href, blog_ids):
                        matched = {"mode":"blog_id","text":text,"href":href}; break
                    elif not blog_ids and title_match_item(text, title):
                        matched = {"mode":"title","text":text}; break
                sample = [{"text":i.get('text','')[:80],"href":i.get('href','')[:80]} for i in items[:5]]
                summary[k] = {"count":len(items),"matched":matched,"items":sample}
            out["sections"] = summary
            out["found"] = [k for k,v in summary.items() if v["matched"]]
            browser.close()
    except Exception as e:
        out["error"] = str(e)
    return jsonify(out)

@app.route("/")
def index():
    return render_template_string(HTML)

# ── HTML ──────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>네이버 블로그 대시보드</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--g:#03C75A;--gd:#02a84a;--gl:#e8f5e9;--bg:#f0f2f5;--card:#fff;--bd:#e4e6ea;--tx:#1a1a1a;--mu:#888;--sm:#555}
body{font-family:-apple-system,BlinkMacSystemFont,'Noto Sans KR',sans-serif;background:var(--bg);color:var(--tx);min-height:100vh}
.hd{background:var(--card);border-bottom:1px solid var(--bd);padding:0 24px;height:56px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.hd-brand{font-size:17px;font-weight:800;display:flex;align-items:center;gap:6px}
.hd-brand em{color:var(--g);font-style:normal}
.hd-right{display:flex;align-items:center;gap:14px}
.hd-stat{font-size:13px;color:var(--mu);display:flex;align-items:center;gap:5px}
.hd-stat strong{color:var(--tx);font-size:16px;font-weight:800}
.btn-hd{padding:6px 14px;background:var(--g);color:#fff;border:none;border-radius:6px;font-size:12px;font-weight:700;cursor:pointer}
.btn-hd:hover{background:var(--gd)}
.btn-hd:disabled{background:#bbb;cursor:not-allowed}
.layout{display:flex;min-height:calc(100vh - 56px)}
/* sidebar */
.sidebar{width:230px;min-width:230px;background:var(--card);border-right:1px solid var(--bd);overflow-y:auto;display:flex;flex-direction:column}
.sb-head{padding:14px;border-bottom:1px solid var(--bd)}
.sb-label{font-size:11px;font-weight:700;color:var(--mu);text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px}
.add-form{display:flex;flex-direction:column;gap:6px}
.add-form input{padding:7px 10px;border:1.5px solid var(--bd);border-radius:6px;font-size:12px;font-family:inherit;outline:none;transition:border-color .15s}
.add-form input:focus{border-color:var(--g)}
.add-form input::placeholder{color:#bbb}
.btn-add{padding:7px;background:var(--g);color:#fff;border:none;border-radius:6px;font-size:12px;font-weight:700;cursor:pointer}
.btn-add:hover{background:var(--gd)}
.acct-list{flex:1;overflow-y:auto;padding:6px}
.acct-item{padding:10px 12px;border-radius:8px;cursor:pointer;border:1.5px solid transparent;margin-bottom:3px;transition:background .12s}
.acct-item:hover{background:#f5f5f5}
.acct-item.active{background:var(--gl);border-color:#a5d6a7}
.acct-name{font-size:13px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.acct-id{font-size:11px;color:var(--mu);margin-top:1px}
.acct-v{font-size:11px;font-weight:700;color:var(--g);margin-top:2px}
.acct-v.none{color:var(--mu);font-weight:400}
/* main */
.main{flex:1;overflow-y:auto;padding:20px 24px;min-width:0}
.empty-state{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:var(--mu);gap:8px;text-align:center}
.empty-state .icon{font-size:40px}
.detail-head{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:16px;gap:10px}
.detail-name{font-size:20px;font-weight:800}
.detail-bid{font-size:13px;color:var(--mu);margin-top:2px}
.detail-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;flex-shrink:0}
.v-chip{padding:5px 13px;background:var(--gl);border:1px solid #a5d6a7;border-radius:20px;font-size:12px;font-weight:700;color:#1b5e20}
.v-chip.dim{background:#f5f5f5;color:var(--mu);border-color:var(--bd)}
/* ctrl bar */
.ctrl{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px 16px;margin-bottom:14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.ctrl-label{font-size:12px;font-weight:700;color:var(--sm);white-space:nowrap}
.kw-seg{display:flex;border:1.5px solid var(--bd);border-radius:6px;overflow:hidden}
.kw-seg button{padding:5px 11px;font-size:12px;font-weight:700;background:#fff;border:none;border-right:1px solid var(--bd);cursor:pointer;color:var(--mu)}
.kw-seg button:last-child{border-right:none}
.kw-seg button.sel{background:var(--g);color:#fff}
.ctrl-sep{width:1px;height:22px;background:var(--bd)}
.btn-sync{padding:6px 12px;background:#fff;border:1.5px solid var(--bd);border-radius:6px;font-size:12px;font-weight:700;cursor:pointer;color:var(--sm);white-space:nowrap}
.btn-sync:hover{border-color:var(--g);color:var(--g)}
.btn-sync:disabled{color:#bbb;cursor:not-allowed}
.btn-check{padding:6px 14px;background:var(--g);color:#fff;border:none;border-radius:6px;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap}
.btn-check:hover{background:var(--gd)}
.btn-check:disabled{background:#bbb;cursor:not-allowed}
.spacer{flex:1}
.sync-meta{font-size:11px;color:var(--mu);white-space:nowrap}
/* progress */
.prog-wrap{height:4px;background:var(--bg);border-radius:2px;overflow:hidden;margin-bottom:10px;display:none}
.prog-bar{height:100%;background:var(--g);border-radius:2px;transition:width .3s;width:0}
.prog-txt{font-size:11px;color:var(--mu);margin-bottom:10px;display:none}
/* table */
.tbl-wrap{background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:auto;-webkit-overflow-scrolling:touch}
table{width:100%;min-width:760px;border-collapse:collapse;font-size:12.5px}
thead th{background:#fafafa;padding:9px 12px;text-align:left;font-size:10.5px;font-weight:700;color:var(--mu);border-bottom:1.5px solid var(--bd);white-space:nowrap;letter-spacing:.04em}
tbody td{padding:9px 12px;border-bottom:1px solid var(--bd);vertical-align:middle}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover td{background:#fafffe}
tbody tr.checking td{background:#fffdf5}
.post-title a{color:var(--tx);text-decoration:none;font-weight:600;line-height:1.4;word-break:keep-all}
.post-title a:hover{color:var(--g)}
.post-kw{font-size:10.5px;color:var(--mu);margin-top:3px}
.post-date{font-size:11px;color:var(--mu);white-space:nowrap}
.tags{display:flex;flex-wrap:wrap;gap:3px}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;white-space:nowrap}
.tag-no{color:#c62828;font-size:11px;font-weight:700}
.tag-dim{color:var(--mu);font-size:11px}
.idx-y{color:#1b5e20;font-weight:700;font-size:12px}
.idx-n{color:#c62828;font-weight:700;font-size:12px}
.spin{display:inline-block;width:10px;height:10px;border:2px solid #e0e0e0;border-top-color:var(--g);border-radius:50%;animation:sp .6s linear infinite;vertical-align:middle;margin-right:3px}
@keyframes sp{to{transform:rotate(360deg)}}
/* btn-del */
.btn-del{padding:4px 9px;font-size:11px;background:none;border:1px solid #ffcdd2;color:#c62828;border-radius:5px;cursor:pointer}
.btn-del:hover{background:#ffebee}
/* toast */
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(10px);background:#222;color:#fff;padding:8px 18px;border-radius:14px;font-size:12.5px;opacity:0;transition:opacity .2s,transform .2s;pointer-events:none;z-index:999}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
@media (max-width: 900px){
  .hd{height:auto;min-height:56px;padding:12px 16px;align-items:flex-start;gap:10px;flex-wrap:wrap}
  .hd-right{width:100%;justify-content:space-between;flex-wrap:wrap;gap:10px}
  .layout{display:block;min-height:auto}
  .sidebar{width:100%;min-width:0;border-right:none;border-bottom:1px solid var(--bd)}
  .acct-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px;padding:10px}
  .acct-item{margin-bottom:0}
  .main{padding:16px}
  .detail-head{flex-direction:column}
  .detail-actions{width:100%}
  .ctrl{padding:12px;gap:8px}
  .ctrl-sep{display:none}
  .spacer{display:none}
  .sync-meta{width:100%;white-space:normal}
  .toast{width:min(calc(100vw - 32px),420px);text-align:center}
}
@media (max-width: 640px){
  .hd-brand{font-size:15px}
  .hd-stat{font-size:12px}
  .hd-stat strong{font-size:14px}
  .btn-hd,.btn-sync,.btn-check,.btn-add{width:100%;justify-content:center}
  .add-form input{font-size:16px}
  .detail-name{font-size:18px}
  .detail-bid{font-size:12px}
  .detail-actions{flex-direction:column;align-items:stretch}
  .ctrl{flex-direction:column;align-items:stretch}
  .ctrl-label{white-space:normal}
  .kw-seg{width:100%}
  .kw-seg button{flex:1}
  .tbl-wrap{margin:0;border-radius:8px;overflow:hidden}
  table{min-width:0;font-size:12px}
  thead{display:none}
  tbody{display:block}
  tbody tr{display:block;padding:10px 0}
  tbody td{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:7px 12px;border-bottom:none;text-align:right}
  tbody td::before{content:attr(data-label);font-size:11px;font-weight:700;color:var(--mu);text-align:left;white-space:nowrap}
  tbody td:first-child{padding-top:2px}
  tbody td:last-child{padding-bottom:2px}
  tbody tr+tr{border-top:1px solid var(--bd)}
  tbody tr:hover td{background:transparent}
  .post-title{max-width:none!important;text-align:left}
  .post-title::before{display:block;margin-bottom:4px}
  .post-title a{display:block}
  .post-date{white-space:normal}
  .tags{justify-content:flex-end}
}
</style>
</head>
<body>
<header class="hd">
  <div class="hd-brand" onclick="goHome()" style="cursor:pointer" title="홈으로">🔍 네이버 <em>블로그</em> 대시보드</div>
  <div class="hd-right">
    <div class="hd-stat">오늘 총 방문자 <strong id="total-v">-</strong></div>
    <button class="btn-hd" id="btn-all-v" onclick="checkAllVisitors()">전체 방문자 확인</button>
  </div>
</header>

<div class="layout">
  <aside class="sidebar">
    <div class="sb-head">
      <div class="sb-label">계정 목록</div>
      <div class="add-form">
        <input id="inp-id" placeholder="블로그 아이디 (예: ommeca)" />
        <input id="inp-name" placeholder="표시 이름 (선택)" />
        <button class="btn-add" onclick="addAccount()">+ 계정 등록</button>
      </div>
    </div>
    <div class="acct-list" id="acct-list"></div>
  </aside>

  <main class="main" id="main-panel">
    <div class="empty-state">
      <div class="icon">📋</div>
      <div style="font-size:14px;font-weight:600">왼쪽에서 계정을 선택하세요</div>
      <div style="font-size:12px">계정 추가 → RSS 동기화 → 전체 확인</div>
    </div>
  </main>
</div>

<div class="toast" id="toast"></div>

<script>
// ── localStorage 저장 (계정/게시물 모두 브라우저에 보관) ──
const LS_KEY = 'naver_dash_v2';
let accounts = [], activeId = null, kwN = 2;
const vCache = new Map();
const checkState = {}; // id -> {es, done, total, mode} — 계정 전환 후에도 유지

function lsLoad(){ try{ return JSON.parse(localStorage.getItem(LS_KEY)||'[]'); }catch{ return []; } }
function lsSave(){ localStorage.setItem(LS_KEY, JSON.stringify(accounts)); }

const TAG_COLORS = {
  'VIEW':{bg:'#e8f5e9',tx:'#1b5e20',bd:'#a5d6a7'},'블로그':{bg:'#e8f5e9',tx:'#2e7d32',bd:'#c8e6c9'},
  '뉴스':{bg:'#fff8e1',tx:'#e65100',bd:'#ffe082'},'카페':{bg:'#fce4ec',tx:'#880e4f',bd:'#f48fb1'},
  '지식IN':{bg:'#ede7f6',tx:'#4527a0',bd:'#b39ddb'},'웹사이트':{bg:'#e8eaf6',tx:'#283593',bd:'#9fa8da'},
  '동영상':{bg:'#ffebee',tx:'#b71c1c',bd:'#ef9a9a'},'쇼핑':{bg:'#fff3e0',tx:'#bf360c',bd:'#ffcc80'},
  '포스트':{bg:'#e0f7fa',tx:'#006064',bd:'#80deea'},
};
function mkTag(s){const c=TAG_COLORS[s]||{bg:'#f5f5f5',tx:'#555',bd:'#e0e0e0'};return`<span class="tag" style="background:${c.bg};color:${c.tx};border:1px solid ${c.bd}">${esc(s)}</span>`;}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2500)}
function fmtDate(s){
  if(!s)return'';
  let m=s.match(/(\d{4})-(\d{2})-(\d{2})/);
  if(m)return`${m[1]}.${m[2]}.${m[3]}`;
  const MO={Jan:'01',Feb:'02',Mar:'03',Apr:'04',May:'05',Jun:'06',Jul:'07',Aug:'08',Sep:'09',Oct:'10',Nov:'11',Dec:'12'};
  m=s.match(/(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})/);
  if(m)return`${m[3]}.${MO[m[2]]||'??'}.${m[1].padStart(2,'0')}`;
  try{const d=new Date(s);if(!isNaN(d))return`${d.getFullYear()}.${String(d.getMonth()+1).padStart(2,'0')}.${String(d.getDate()).padStart(2,'0')}`;}catch(e){}
  return s.slice(0,10);
}
function genId(){return Math.random().toString(36).slice(2,10)+Date.now().toString(36);}

function updateTotalV(){
  const total=[...vCache.values()].reduce((s,v)=>s+v,0);
  document.getElementById('total-v').textContent=vCache.size?total.toLocaleString('ko-KR')+'명':'-';
}

async function apiFetch(url,opts={}){
  const r=await fetch(url,{headers:{'Content-Type':'application/json'},...opts});
  return r.json();
}

// ── 사이드바 ──
function renderSidebar(){
  const el=document.getElementById('acct-list');
  if(!accounts.length){el.innerHTML='<div style="padding:14px;font-size:12px;color:var(--mu);text-align:center">등록된 계정이 없습니다</div>';return;}
  el.innerHTML=accounts.map(a=>{
    const v=vCache.get(a.id);
    const cs=checkState[a.id];
    let statusHtml;
    if(cs) statusHtml=`<div class="acct-v" style="color:#e65100"><span class="spin"></span> 확인 중 ${cs.done}/${cs.total}</div>`;
    else if(v!==undefined) statusHtml=`<div class="acct-v">${v.toLocaleString('ko-KR')}명 방문</div>`;
    else statusHtml=`<div class="acct-v none">방문자 미확인</div>`;
    return `<div class="acct-item${a.id===activeId?' active':''}" onclick="selectAccount('${a.id}')">
      <div class="acct-name">${esc(a.displayName)}</div>
      <div class="acct-id">${esc(a.blogId)}</div>
      ${statusHtml}
    </div>`;
  }).join('');
}

function addAccount(){
  const blogId=document.getElementById('inp-id').value.trim().toLowerCase();
  const displayName=document.getElementById('inp-name').value.trim();
  if(!blogId){toast('블로그 아이디를 입력하세요');return;}
  if(accounts.some(a=>a.blogId===blogId)){toast('이미 등록된 아이디입니다');return;}
  const account={id:genId(),blogId,displayName:displayName||blogId,posts:[],lastSync:null};
  accounts.push(account);
  lsSave();
  document.getElementById('inp-id').value='';document.getElementById('inp-name').value='';
  toast('"'+account.displayName+'" 등록 완료');
  renderSidebar();selectAccount(account.id);
}

function deleteAccount(id,name){
  if(!confirm('"'+name+'" 계정을 삭제할까요?'))return;
  if(checkState[id]){checkState[id].es.close();delete checkState[id];}
  accounts=accounts.filter(a=>a.id!==id);
  lsSave();
  if(activeId===id){
    activeId=null;
    document.getElementById('main-panel').innerHTML='<div class="empty-state"><div class="icon">📋</div><div style="font-size:14px;font-weight:600">왼쪽에서 계정을 선택하세요</div></div>';
  }
  toast('계정 삭제됨');renderSidebar();
}

function selectAccount(id){
  activeId=id;renderSidebar();
  const a=accounts.find(x=>x.id===id);if(a)renderDetail(a);
}

// ── 상세 패널 ──
function renderDetail(account){
  const posts=account.posts||[];
  const v=vCache.get(account.id);
  const vHtml=v!==undefined?`<div class="v-chip">오늘 방문자: ${v.toLocaleString('ko-KR')}명</div>`:`<div class="v-chip dim">방문자 미확인</div>`;
  const lastSync=account.lastSync?'동기화: '+account.lastSync.slice(0,16).replace('T',' '):'미동기화';
  const cs=checkState[account.id];
  const running=!!cs;
  const cmode=cs?cs.mode:'all';
  const pct=cs&&cs.total?Math.round(cs.done/cs.total*100):0;
  const hasPosts=!!posts.length;
  document.getElementById('main-panel').innerHTML=`
    <div class="detail-head">
      <div>
        <div class="detail-name">${esc(account.displayName)}</div>
        <div class="detail-bid">blog.naver.com/${esc(account.blogId)}</div>
      </div>
      <div class="detail-actions">
        <div id="v-chip">${vHtml}</div>
        <button class="btn-sync" id="btn-v" onclick="checkVisitor('${account.id}','${account.blogId}')">방문자 확인</button>
        <button class="btn-del" onclick="deleteAccount('${account.id}','${esc(account.displayName)}')">삭제</button>
      </div>
    </div>
    <div class="ctrl">
      <span class="ctrl-label">키워드 단어 수</span>
      <div class="kw-seg" id="kw-seg">
        ${[1,2,3,4,5].map(n=>`<button onclick="setKwN(${n},this)"${n===kwN?' class="sel"':''}>${n}</button>`).join('')}
      </div>
      <div class="ctrl-sep"></div>
      <button class="btn-sync" id="btn-sync" onclick="syncPosts('${account.id}','${account.blogId}')"${running?' disabled':''}>RSS 동기화</button>
      <div class="spacer"></div>
      <span class="sync-meta" id="sync-meta">${lastSync} · ${posts.length}개</span>
      <button class="btn-sync" id="btn-idx" onclick="startCheck('${account.id}','${account.blogId}','index')"${!hasPosts||running?' disabled':''}>색인 확인</button>
      <button class="btn-sync" id="btn-exp" onclick="startCheck('${account.id}','${account.blogId}','exposure')"${!hasPosts||running?' disabled':''}>노출 확인</button>
      <button class="btn-check" id="btn-check" onclick="startCheck('${account.id}','${account.blogId}','all')"${!hasPosts||running?' disabled':''}>전체 확인</button>
    </div>
    <div class="prog-wrap" id="prog-wrap" style="${running?'':'display:none'}"><div class="prog-bar" id="prog-bar" style="width:${pct}%"></div></div>
    <div class="prog-txt" id="prog-txt" style="${running?'':'display:none'}">${running?cs.done+' / '+cs.total+' 완료':''}</div>
    <div class="tbl-wrap">
      ${hasPosts?renderTable(posts,running,cmode,account.id.slice(0,6)):'<div style="padding:24px;text-align:center;color:var(--mu);font-size:13px">RSS 동기화를 눌러 게시물을 불러오세요</div>'}
    </div>`;
}

function renderTable(posts,running,mode,aid){
  // aid: 계정별 prefix로 element ID 충돌 방지
  const p_=aid||'x';
  const rows=posts.map((p,i)=>{
    const hasIdx=p.blogIndex!==undefined;
    const hasExp=p.pc!==undefined;
    let idxHtml,pcHtml,mobHtml;
    if(hasIdx){
      idxHtml=p.blogIndex===true?'<span class="idx-y">✓ 색인됨</span>':p.blogIndex===false?'<span class="idx-n">✗ 미색인</span>':'<span class="tag-dim">—</span>';
    }else if(running&&mode!=='exposure'){
      idxHtml='<span class="spin"></span>';
    }else{
      idxHtml='<span class="tag-dim">—</span>';
    }
    if(hasExp){
      pcHtml=p.pc&&p.pc.length?'<div class="tags">'+p.pc.map(mkTag).join('')+'</div>':'<span class="tag-no">✗ 미노출</span>';
      mobHtml=p.mobile&&p.mobile.length?'<div class="tags">'+p.mobile.map(mkTag).join('')+'</div>':'<span class="tag-no">✗ 미노출</span>';
    }else if(running&&mode!=='index'){
      pcHtml=mobHtml='<span class="spin"></span>';
    }else{
      pcHtml=mobHtml='<span class="tag-dim">—</span>';
    }
    const kwText=p.keyword?'키워드: '+p.keyword:'키워드: —';
    const pendingIdx=running&&!hasIdx&&mode!=='exposure';
    const pendingExp=running&&!hasExp&&mode!=='index';
    return `<tr id="row-${p_}-${i}"${pendingIdx||pendingExp?' class="checking"':''}>
      <td data-label="#" style="color:var(--mu);font-size:11px;width:26px;text-align:center">${i+1}</td>
      <td class="post-title" data-label="?쒕ぉ / ?ㅼ썙??" style="max-width:280px">
        ${p.link?`<a href="${esc(p.link)}" target="_blank" rel="noopener">${esc(p.title)}</a>`:esc(p.title)}
        <div class="post-kw" id="kw-${p_}-${i}">${esc(kwText)}</div>
      </td>
      <td class="post-date" data-label="諛쒗뻾??">${fmtDate(p.pubDate)}</td>
      <td data-label="釉붾줈洹??됱씤" id="idx-${p_}-${i}">${idxHtml}</td>
      <td data-label="PC ?몄텧" id="pc-${p_}-${i}">${pcHtml}</td>
      <td data-label="紐⑤컮???몄텧" id="mob-${p_}-${i}">${mobHtml}</td>
    </tr>`;
  }).join('');
  return `<table><thead><tr>
    <th>#</th><th>제목 / 키워드</th><th>발행일</th>
    <th>블로그 색인</th><th>🖥 PC 노출</th><th>📱 모바일 노출</th>
  </tr></thead><tbody>${rows}</tbody></table>`;
}

function setKwN(n,btn){kwN=n;document.querySelectorAll('#kw-seg button').forEach(b=>b.classList.remove('sel'));btn.classList.add('sel');}

async function syncPosts(id,blogId){
  const btn=document.getElementById('btn-sync');
  if(btn){btn.disabled=true;btn.textContent='동기화 중...';}
  const r=await apiFetch('/api/sync',{method:'POST',body:JSON.stringify({blogId,limit:30})});
  if(r.error){toast('RSS 오류: '+r.error);if(btn){btn.disabled=false;btn.textContent='RSS 동기화';}return;}
  const a=accounts.find(x=>x.id===id);
  if(a){a.posts=r.posts;a.lastSync=new Date().toISOString().slice(0,19);lsSave();}
  toast(r.count+'개 게시물 불러옴');
  renderDetail(accounts.find(x=>x.id===id));
}

async function checkVisitor(id,blogId){
  const btn=document.getElementById('btn-v');
  const chip=document.getElementById('v-chip');
  if(btn)btn.disabled=true;
  if(chip)chip.innerHTML='<div class="v-chip dim"><span class="spin"></span>확인 중...</div>';
  const r=await apiFetch('/api/visitor',{method:'POST',body:JSON.stringify({blogId})});
  if(btn)btn.disabled=false;
  if(r.today!==null&&r.today!==undefined){
    vCache.set(id,r.today);updateTotalV();renderSidebar();
    if(chip)chip.innerHTML=`<div class="v-chip">오늘 방문자: ${Number(r.today).toLocaleString('ko-KR')}명</div>`;
  } else {
    if(chip)chip.innerHTML='<div class="v-chip dim">방문자 찾기 실패</div>';
    toast('방문자 수를 찾지 못했습니다');
  }
}

async function checkAllVisitors(){
  const btn=document.getElementById('btn-all-v');
  btn.disabled=true;
  for(let i=0;i<accounts.length;i++){
    btn.textContent=`확인 중 (${i+1}/${accounts.length})`;
    const r=await apiFetch('/api/visitor',{method:'POST',body:JSON.stringify({blogId:accounts[i].blogId})});
    if(r.today!==null&&r.today!==undefined){vCache.set(accounts[i].id,r.today);updateTotalV();renderSidebar();}
  }
  btn.disabled=false;btn.textContent='전체 방문자 확인';
  toast('전체 방문자 확인 완료');
}

async function startCheck(id,blogId,mode){
  mode=mode||'all';
  if(checkState[id]){checkState[id].es.close();delete checkState[id];}
  const account=accounts.find(a=>a.id===id);
  if(!account||!account.posts.length){toast('RSS 동기화 먼저 해주세요');return;}
  // 새 확인 시작 시 이전 결과 전체 초기화 (계정간 겹침 방지)
  account.posts.forEach(p=>{
    delete p.blogIndex;delete p.pc;delete p.mobile;delete p.keyword;delete p.error;
  });
  lsSave();
  // API 호출 전에 checkState 세팅 → 스피너 즉시 표시
  checkState[id]={done:0,total:account.posts.length,mode,es:null};
  if(activeId===id)renderDetail(account);
  renderSidebar();
  const posts=account.posts;
  const r=await apiFetch('/api/check',{method:'POST',body:JSON.stringify({blogId,posts,kwN,mode})});
  if(r.error){
    delete checkState[id];
    toast('오류: '+r.error);
    if(activeId===id)renderDetail(accounts.find(a=>a.id===id));
    renderSidebar();return;
  }
  const total=r.total;
  if(checkState[id])checkState[id].total=total;
  renderSidebar();
  const es=new EventSource('/stream/'+r.job_id);
  checkState[id].es=es;
  es.onmessage=ev=>{
    if(ev.data==='[DONE]'||ev.data==='[TIMEOUT]'){
      es.close();
      const cs=checkState[id];const doneN=cs?cs.done:total;
      delete checkState[id];
      if(activeId===id){
        ['btn-check','btn-idx','btn-exp','btn-sync'].forEach(bid=>{const b=document.getElementById(bid);if(b)b.disabled=false;});
        const pt=document.getElementById('prog-txt');if(pt)pt.textContent='✓ '+doneN+'/'+total+' 완료';
      }
      renderSidebar();return;
    }
    const res=JSON.parse(ev.data);
    const i=res.idx;
    if(checkState[id])checkState[id].done++;
    // 결과를 계정 데이터에 저장 (계정 전환 후 돌아와도 보임)
    const acct=accounts.find(a=>a.id===id);
    if(acct&&acct.posts[i]){
      if(mode!=='index'){acct.posts[i].pc=res.pc;acct.posts[i].mobile=res.mobile;acct.posts[i].keyword=res.keyword;}
      if(mode!=='exposure'){acct.posts[i].blogIndex=res.blog_index;}
      if(res.error)acct.posts[i].error=res.error;
      lsSave();
    }
    renderSidebar();
    if(activeId!==id)return; // 다른 계정 보는 중이면 DOM 업데이트 생략
    const cs=checkState[id];const doneN=cs?cs.done:0;
    const pct=total?Math.round(doneN/total*100):0;
    const pb=document.getElementById('prog-bar');if(pb)pb.style.width=pct+'%';
    const pt=document.getElementById('prog-txt');if(pt)pt.textContent=doneN+' / '+total+' 완료';
    // 계정별 prefix로 올바른 DOM 요소만 접근
    const p_=id.slice(0,6);
    const row=document.getElementById('row-'+p_+'-'+i);if(row)row.className='';
    if(mode!=='index'){const kwEl=document.getElementById('kw-'+p_+'-'+i);if(kwEl)kwEl.textContent='키워드: '+(res.keyword||'');}
    if(mode!=='exposure'){
      const idxEl=document.getElementById('idx-'+p_+'-'+i);
      if(idxEl)idxEl.innerHTML=res.blog_index===true?'<span class="idx-y">✓ 색인됨</span>':res.blog_index===false?'<span class="idx-n">✗ 미색인</span>':'<span class="tag-dim">—</span>';
    }
    if(mode!=='index'){
      const pcEl=document.getElementById('pc-'+p_+'-'+i);
      if(pcEl)pcEl.innerHTML=res.error&&!(res.pc&&res.pc.length)?'<span style="color:#e53935;font-size:11px">오류</span>':res.pc&&res.pc.length?'<div class="tags">'+res.pc.map(mkTag).join('')+'</div>':'<span class="tag-no">✗ 미노출</span>';
      const mobEl=document.getElementById('mob-'+p_+'-'+i);
      if(mobEl)mobEl.innerHTML=res.mobile&&res.mobile.length?'<div class="tags">'+res.mobile.map(mkTag).join('')+'</div>':'<span class="tag-no">✗ 미노출</span>';
    }
  };
  es.onerror=()=>{
    es.close();delete checkState[id];
    if(activeId===id){['btn-check','btn-idx','btn-exp','btn-sync'].forEach(bid=>{const b=document.getElementById(bid);if(b)b.disabled=false;});}
    renderSidebar();
  };
}

function goHome(){
  activeId=null;renderSidebar();
  document.getElementById('main-panel').innerHTML='<div class="empty-state"><div class="icon">📋</div><div style="font-size:14px;font-weight:600">왼쪽에서 계정을 선택하세요</div><div style="font-size:12px">계정 추가 → RSS 동기화 → 전체 확인</div></div>';
}

// ── 초기화 ──
accounts=lsLoad();
renderSidebar();
if(accounts.length){selectAccount(accounts[0].id);}
</script>
</body>
</html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"  Dashboard  ->  http://localhost:{port}")
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
