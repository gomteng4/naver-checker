#!/usr/bin/env python3
"""
네이버 통합검색 노출 확인 웹앱
실행: python app.py  ->  http://localhost:5001
"""
from flask import Flask, render_template_string, request, jsonify, Response
from playwright.sync_api import sync_playwright
import urllib.parse, threading, queue, uuid, time, json, re, os

app = Flask(__name__)
jobs = {}

SECTION_MAP = {
    "view":"VIEW","blog":"\ube14\ub85c\uadf8","news":"\ub274\uc2a4",
    "cafearticle":"\uce74\ud398","cafe":"\uce74\ud398","kin":"\uc9c0\uc2ddIN",
    "webkr":"\uc6f9\uc0ac\uc774\ud2b8","site":"\uc6f9\uc0ac\uc774\ud2b8",
    "image":"\uc774\ubbf8\uc9c0","video":"\ub3d9\uc601\uc0c1","shopping":"\uc1fc\ud551",
    "local":"\uc9c0\ub3c4/\ud50c\ub808\uc774\uc2a4","map":"\uc9c0\ub3c4",
    "place":"\ud50c\ub808\uc774\uc2a4","post":"\ud3ec\uc2a4\ud2b8",
    "book":"\ucc45","dict":"\uc5b4\ud559\uc0ac\uc804","academ":"\ud559\uc220\uc815\ubcf4",
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
    s = re.sub(r'[\s\u00b7\u2022\u30fb,.\-_/\\\'\"!?~\[\]()]+', ' ', s or '')
    return s.lower().strip()

def blog_id_match(href, blog_ids):
    """href 가 blog_ids 중 하나에 속하는지 확인 (정확한 ID 세그먼트 매칭)"""
    if not href or not blog_ids: return False
    h = href.lower()
    for bid in blog_ids:
        bid = bid.lower().strip()
        if not bid: continue
        # blog.naver.com/아이디/ 또는 m.blog.naver.com/아이디/
        if re.search(r'blog\.naver\.com/' + re.escape(bid) + r'(/|$|\?)', h):
            return True
    return False

def title_match_item(item_text, target_title):
    """개별 포스트 제목 vs 내 제목 (엄격한 매칭, 오탐 최소화)"""
    if not item_text or not target_title: return False
    ni = normalize(item_text)
    nt = normalize(target_title)
    if nt in ni: return True
    if len(ni) >= 6 and ni in nt: return True
    key_t = nt.replace(' ',''); key_i = ni.replace(' ','')
    if len(key_t) >= 6 and key_t in key_i: return True
    if len(key_i) >= 6 and key_i in key_t: return True
    words = [w for w in nt.split() if len(w) >= 2]
    if len(words) < 2: return False
    matched = sum(1 for w in words if w in ni)
    return matched / len(words) >= 0.65


# href + text 쌍으로 추출 (블로그 아이디 매칭에 필요)
EXTRACT_JS = """
() => {
    const result = {};
    function addItem(type, text, href) {
        const t = (text||'').replace(/\\s+/g,' ').trim();
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
        '[data-type],[id^="sp_"],[id^="section_"],' +
        '[class*="cs_blog"],[class*="cs_news"],[class*="cs_cafe"],' +
        '[class*="cs_view"],[class*="sc_new"],[class*="sc_blog"]'
    ).forEach(container => {
        const type = getSectionType(container);
        if (!type) return;
        if (seen.has(container)) return;
        container.querySelectorAll('*').forEach(c => seen.add(c));
        try {
            container.querySelectorAll(TITLE_SELS).forEach(el => {
                const t=(el.innerText||el.textContent||'').replace(/\\s+/g,' ').trim();
                const h=el.href||el.getAttribute('href')||'';
                if (t.length>=5 && t.length<=300) addItem(type, t, h);
            });
        } catch(e) {}
    });

    // fallback: PC는 #main_pack, 모바일은 #ct 사용
    const mainEl = document.querySelector('#main_pack') ||
                   document.querySelector('#ct') ||
                   document.querySelector('#container') ||
                   document.body;
    if (mainEl && Object.keys(result).length === 0) {
        // 섹션 구분 없이 링크 타입으로 분류
        mainEl.querySelectorAll('a[href]').forEach(a => {
            const h=(a.href||'').toLowerCase(), t=(a.innerText||a.textContent||'').replace(/\\s+/g,' ').trim();
            if (t.length<5||t.length>300) return;
            if (h.includes('blog.naver')) addItem('blog',t,a.href);
            else if (h.includes('cafe.naver')) addItem('cafearticle',t,a.href);
            else if (h.includes('post.naver')) addItem('post',t,a.href);
            else if (h.includes('kin.naver')) addItem('kin',t,a.href);
        });
    } else if (mainEl && Object.keys(result).length > 0) {
        // PC에서 섹션은 잡혔지만 blog 링크도 별도 보강
        mainEl.querySelectorAll('a[href*="blog.naver"]').forEach(a => {
            const t=(a.innerText||a.textContent||'').replace(/\\s+/g,' ').trim();
            if (t.length>=5&&t.length<=300) addItem('blog',t,a.href);
        });
    }
    return result;
}
"""


def find_title_in_sections(page, title, blog_ids=None):
    try: page.wait_for_load_state("networkidle", timeout=18000)
    except: pass
    try: page.wait_for_selector("#main_pack,#ct,#container,.api_subject_bx,#wrap_search,[id^='sp_']", timeout=8000)
    except: pass
    time.sleep(2.0)
    try:
        section_items = page.evaluate(EXTRACT_JS)
    except Exception as e:
        return [], f"JS\uc624\ub958: {str(e)[:80]}"

    use_blog_id = bool(blog_ids)
    found_sections, seen = [], set()

    for raw_type, items in section_items.items():
        section_name = map_section(raw_type)
        if not section_name or section_name in seen: continue
        for item in items:
            text = item.get('text','') if isinstance(item,dict) else item
            href = item.get('href','') if isinstance(item,dict) else ''
            if use_blog_id:
                # 블로그 아이디 모드: URL이 내 블로그인지만 확인
                if blog_id_match(href, blog_ids):
                    found_sections.append(section_name)
                    seen.add(section_name)
                    print(f"  [ID MATCH] {raw_type} -> {href[:70]}")
                    break
            else:
                # 아이디 없으면 제목 비교
                if title_match_item(text, title):
                    found_sections.append(section_name)
                    seen.add(section_name)
                    print(f"  [TITLE MATCH] {raw_type} -> '{text[:60]}'")
                    break

    if not found_sections:
        total = sum(len(v) for v in section_items.values())
        print(f"  [NO MATCH] sections={list(section_items.keys())}, items={total}")
    return found_sections, None


def crawl_keyword(keyword, title, pc_ctx, mobile_ctx, blog_ids=None):
    encoded = urllib.parse.quote(keyword)
    result = {"keyword":keyword,"title":title,"pc":[],"mobile":[],"error":None}
    print(f"\n[CRAWL] {keyword}")
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
        # 모바일은 networkidle 대신 domcontentloaded (모바일 페이지는 무한 로딩 많음)
        try:
            page.goto(f"https://m.search.naver.com/search.naver?query={encoded}", wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass  # 타임아웃돼도 부분 로딩된 상태로 계속 진행
        result["mobile"], err = find_title_in_sections(page, title, blog_ids)
        if err and not result["error"]: result["error"] = err
        page.close()
    except Exception as e:
        if not result["error"]: result["error"] = str(e)[:100]
    print(f"  => pc={result['pc']}, mobile={result['mobile']}")
    return result


STEALTH_JS = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});
Object.defineProperty(navigator,'languages',{get:()=>['ko-KR','ko','en-US','en']});
window.chrome={runtime:{}};
"""

def run_job(job_id, items, blog_ids=None):
    q = jobs[job_id]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                "--no-sandbox","--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"])
            pc_ctx = browser.new_context(
                viewport={"width":1920,"height":1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="ko-KR",
                extra_http_headers={"Accept-Language":"ko-KR,ko;q=0.9,en-US;q=0.8"},
            )
            pc_ctx.add_init_script(STEALTH_JS)
            mobile_ctx = browser.new_context(
                viewport={"width":390,"height":844},
                user_agent="Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36",
                is_mobile=True, has_touch=True, locale="ko-KR",
                extra_http_headers={"Accept-Language":"ko-KR,ko;q=0.9,en-US;q=0.8"},
            )
            mobile_ctx.add_init_script(STEALTH_JS)
            for keyword, title in items:
                res = crawl_keyword(keyword, title, pc_ctx, mobile_ctx, blog_ids)
                q.put(("result", res))
                time.sleep(1.2)
            browser.close()
    except Exception as e:
        q.put(("error", str(e)))
    q.put(("done", None))


@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/start", methods=["POST"])
def start():
    data = request.json or {}
    items = []
    for line in data.get("lines", []):
        line = line.strip()
        if not line: continue
        if "|" in line:
            parts = line.split("|", 1)
            keyword, title = parts[0].strip(), parts[1].strip()
        else:
            keyword = title = line
        if keyword: items.append((keyword, title))
    if not items: return jsonify({"error":"\uc785\ub825\uc5c6\uc74c"}), 400
    blog_ids = [b.strip() for b in data.get("blog_ids", []) if b.strip()]
    job_id = str(uuid.uuid4())
    jobs[job_id] = queue.Queue()
    threading.Thread(target=run_job, args=(job_id, items, blog_ids or None), daemon=True).start()
    return jsonify({"job_id":job_id,"total":len(items),"blog_id_mode":bool(blog_ids)})

@app.route("/stream/<job_id>")
def stream(job_id):
    q = jobs.get(job_id)
    if not q: return jsonify({"error":"\uc5c6\uc74c"}), 404
    def gen():
        while True:
            try: t, payload = q.get(timeout=90)
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


HTML = r"""
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>네이버 통합검색 노출 확인</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--g:#03C75A;--gd:#02a84a;--bg:#f0f2f5;--card:#fff;--bd:#e8e8e8;--tx:#1a1a1a;--mu:#888}
body{font-family:-apple-system,BlinkMacSystemFont,'Noto Sans KR',sans-serif;background:var(--bg);color:var(--tx);padding:0 0 40px}
.wrap{max-width:1200px;margin:0 auto;padding:0 16px}
.hd{padding:22px 0 0}
.hd h1{font-size:20px;font-weight:800}
.hd h1 em{color:var(--g);font-style:normal}
.hd p{color:var(--mu);font-size:13px;margin-top:3px}
.tab-bar{display:flex;gap:0;border-bottom:2px solid var(--bd);margin:18px 0 22px}
.tab-btn{padding:10px 22px;font-size:14px;font-weight:700;background:none;border:none;border-bottom:3px solid transparent;margin-bottom:-2px;cursor:pointer;color:var(--mu);transition:color .15s,border-color .15s}
.tab-btn.active{color:var(--g);border-bottom-color:var(--g)}
.tab-btn:hover:not(.active){color:var(--tx)}
.tab-btn.cfg{margin-left:auto;font-size:13px}
.tab-pane{display:none}.tab-pane.active{display:block}
.card{background:var(--card);border-radius:14px;padding:22px;box-shadow:0 1px 6px rgba(0,0,0,.08);margin-bottom:18px}
.clabel{font-size:11px;font-weight:700;color:var(--mu);text-transform:uppercase;letter-spacing:.07em;margin-bottom:10px}
.fmt-box{background:#f8fffe;border:1px solid #c8f0dc;border-radius:8px;padding:10px 14px;font-size:12px;color:#1b5e20;margin-bottom:10px;line-height:1.7}
.info-box{background:#f5f5ff;border:1px solid #d0d0f0;border-radius:8px;padding:10px 14px;font-size:12px;color:#333;margin-bottom:12px;line-height:1.7}
.warn-box{background:#fff8e1;border:1px solid #ffe082;border-radius:8px;padding:10px 14px;font-size:12px;color:#795548;margin-bottom:12px;line-height:1.7}
textarea{width:100%;height:220px;border:1.5px solid var(--bd);border-radius:10px;padding:12px 14px;font-size:13px;line-height:1.8;resize:vertical;font-family:inherit;outline:none;transition:border-color .15s}
textarea:focus{border-color:var(--g)}
textarea.out-ta{height:180px;background:#fafafa;font-size:12.5px}
textarea.ids-ta{height:100px;font-size:13px;font-family:monospace}
.bar{display:flex;justify-content:space-between;align-items:center;margin-top:8px}
.hint{font-size:12px;color:var(--mu)}
.cnt{font-size:12px;font-weight:700;color:var(--g)}
.actions{margin-top:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.btn{padding:10px 24px;background:var(--g);color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;transition:background .15s}
.btn:hover{background:var(--gd)}
.btn:disabled{background:#bbb;cursor:not-allowed}
.btn-sm{padding:7px 14px;font-size:12px}
.btn-out{background:transparent;border:1.5px solid var(--bd);color:var(--tx);font-weight:600}
.btn-out:hover{background:var(--bg)}
.btn-blue{background:#1565c0}.btn-blue:hover{background:#0d47a1}
.id-badge{display:inline-flex;align-items:center;gap:4px;padding:3px 10px 3px 10px;background:#e8f5e9;border:1px solid #a5d6a7;border-radius:20px;font-size:12px;font-weight:700;color:#1b5e20;margin:2px}
.id-badge .del{cursor:pointer;color:#888;font-weight:400;margin-left:2px;line-height:1}
.id-badge .del:hover{color:#c62828}
.id-chips{display:flex;flex-wrap:wrap;gap:4px;min-height:28px;margin-top:8px}
.mode-badge{display:inline-flex;align-items:center;gap:5px;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:700}
.mode-badge.id{background:#e8f5e9;color:#1b5e20;border:1px solid #a5d6a7}
.mode-badge.ttl{background:#fff8e1;color:#795548;border:1px solid #ffe082}
.kw-opts{margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.kw-opts label{font-size:13px;font-weight:600;color:var(--tx)}
.kw-seg{display:flex;border:1.5px solid var(--bd);border-radius:8px;overflow:hidden}
.kw-seg button{padding:6px 14px;font-size:13px;font-weight:700;background:#fff;border:none;border-right:1px solid var(--bd);cursor:pointer;color:var(--mu)}
.kw-seg button:last-child{border-right:none}
.kw-seg button.sel{background:var(--g);color:#fff}
.toast{position:fixed;bottom:28px;left:50%;transform:translateX(-50%) translateY(20px);background:#222;color:#fff;padding:9px 20px;border-radius:20px;font-size:13px;opacity:0;transition:opacity .2s,transform .2s;pointer-events:none;z-index:999}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.pb-wrap{height:5px;background:var(--bg);border-radius:3px;overflow:hidden;margin-bottom:10px}
.pb{height:100%;background:var(--g);border-radius:3px;transition:width .4s;width:0}
.pt{font-size:13px;color:var(--mu)}.pt strong{color:var(--tx)}
.pkw{font-size:11px;color:var(--mu);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
thead th{background:#fafafa;padding:9px 13px;text-align:left;font-size:11px;font-weight:700;color:var(--mu);border-bottom:1.5px solid var(--bd);white-space:nowrap;text-transform:uppercase;letter-spacing:.05em}
tbody td{padding:10px 13px;border-bottom:1px solid var(--bd);vertical-align:middle}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover td{background:#f6fffb}
.tn{color:var(--mu);font-size:12px;width:30px}
.tkw{font-weight:600;max-width:200px;word-break:keep-all;line-height:1.4;font-size:12px}
.nsr{display:flex;gap:4px;margin-top:6px;flex-wrap:wrap}
.nsr-btn{padding:2px 8px;font-size:10.5px;font-weight:700;border-radius:5px;cursor:pointer;border:1.5px solid var(--bd);background:#fff;color:var(--mu);white-space:nowrap;line-height:1.6;text-decoration:none}
.nsr-btn.kw{border-color:#03C75A;color:#03C75A}.nsr-btn.kw:hover{background:#03C75A;color:#fff}
.nsr-btn.ttl{border-color:#1565c0;color:#1565c0}.nsr-btn.ttl:hover{background:#1565c0;color:#fff}
.ttl{max-width:260px;word-break:keep-all;font-size:12px;color:#444;line-height:1.4}
.ts{max-width:300px}
.tags{display:flex;flex-wrap:wrap;gap:4px}
.tag{display:inline-block;padding:3px 9px;border-radius:20px;font-size:11px;font-weight:700;white-space:nowrap}
.tag-ng{color:#c62828;font-size:12px;font-weight:700}
.row-wait td{background:#fffdf5!important}
.row-err td{background:#fff5f5!important}
.spin{display:inline-block;width:11px;height:11px;border:2px solid #e0e0e0;border-top-color:var(--g);border-radius:50%;animation:sp .6s linear infinite;vertical-align:middle;margin-right:4px}
@keyframes sp{to{transform:rotate(360deg)}}
.rmeta{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:8px}
.rtitle{font-size:15px;font-weight:700}
.rsub{font-size:12px;color:var(--mu);margin-top:2px}
#resCard{display:none}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:14px;font-size:12px;color:var(--mu)}
.preview-rows{margin-top:10px;max-height:200px;overflow-y:auto;border:1.5px solid var(--bd);border-radius:10px;background:#fafafa}
.preview-row{display:flex;align-items:stretch;border-bottom:1px solid var(--bd);font-size:12.5px}
.preview-row:last-child{border-bottom:none}
.pr-num{width:28px;min-width:28px;display:flex;align-items:center;justify-content:center;color:var(--mu);font-size:11px;border-right:1px solid var(--bd);background:#f5f5f5}
.pr-kw{padding:7px 10px;font-weight:700;color:#1b5e20;border-right:1px solid var(--bd);min-width:110px;max-width:180px;word-break:keep-all;line-height:1.4}
.pr-sep{padding:7px 5px;color:var(--mu);font-size:11px;border-right:1px solid var(--bd);display:flex;align-items:center}
.pr-ttl{padding:7px 10px;color:#333;line-height:1.4;word-break:keep-all;flex:1}
</style>
</head>
<body>
<div class="wrap">
  <div class="hd">
    <h1>🔍 네이버 <em>통합검색</em> 노출 확인</h1>
    <p>검색 키워드로 검색 → 내 블로그 글이 어느 영역에 노출되는지 PC·모바일 각각 확인</p>
  </div>
  <div class="tab-bar">
    <button class="tab-btn active" onclick="switchTab('keygen',this)">✏️ 키워드 생성</button>
    <button class="tab-btn" onclick="switchTab('search',this)">🔍 검색 확인</button>
    <button class="tab-btn cfg" onclick="switchTab('config',this)">⚙️ 블로그 설정</button>
  </div>

  <!-- TAB: 검색 확인 -->
  <div id="tab-search" class="tab-pane">
    <!-- 블로그 아이디 상태 표시 -->
    <div class="card" id="idStatusCard" style="padding:14px 22px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span style="font-size:13px;font-weight:700">판정 기준:</span>
        <span class="mode-badge id" id="idModeBadge" style="display:none">🎯 블로그 아이디로 정확 판정</span>
        <span class="mode-badge ttl" id="ttlModeBadge"">⚠️ 제목 유사도 판정 (오탐 가능)</span>
        <div id="savedChips" class="id-chips" style="margin:0"></div>
        <a href="#" style="font-size:12px;color:var(--g);text-decoration:none;margin-left:auto" onclick="switchTab('config',null);return false">⚙️ 블로그 아이디 설정하기 →</a>
      </div>
    </div>
    <div class="card">
      <div class="clabel">검색 키워드 &amp; 글 제목 입력</div>
      <div class="fmt-box">
        <strong>형식:</strong> 검색 키워드 | 글 제목 &nbsp;(한 줄에 하나)<br>
        <strong>예시:</strong> 봉명동선불유심 | 봉명동선불유심 약정 없이 쓰기 좋은 이유 세 가지
      </div>
      <textarea id="inp" placeholder="봉명동선불유심 | 봉명동선불유심 약정 없이 쓰기 좋은 이유 세 가지
산남동선불폰 | 산남동선불폰 개통 전에 꼭 알아야 할 것들" oninput="onInp()"></textarea>
      <div class="bar">
        <span class="hint">※ 파이프( | )로 키워드와 글 제목 구분</span>
        <span class="cnt" id="cnt">0줄</span>
      </div>
      <div class="actions">
        <button class="btn" id="startBtn" onclick="go()">▶ 검색 시작</button>
      </div>
    </div>
    <div class="card" id="progCard" style="display:none">
      <div class="pb-wrap"><div class="pb" id="pb"></div></div>
      <div class="pt"><strong id="pdone">0</strong> / <strong id="ptot">0</strong> 완료</div>
      <div class="pkw" id="pkw">시작 중...</div>
    </div>
    <div class="card" id="resCard">
      <div class="rmeta">
        <div><div class="rtitle">검색 결과</div><div class="rsub" id="rsub"></div></div>
        <button class="btn btn-sm btn-out" onclick="exportCSV()">📥 CSV 저장</button>
      </div>
      <div class="legend">
        <span>🖥 PC</span><span>📱 모바일</span>
        <span style="color:#1b5e20">● 노출됨</span><span style="color:#c62828">● 미노출</span>
      </div>
      <div class="tw"><table>
        <thead><tr>
          <th>#</th><th>검색 키워드</th><th>글 제목</th>
          <th>🖥 PC 노출 영역</th><th>📱 모바일 노출 영역</th>
        </tr></thead>
        <tbody id="tbody"></tbody>
      </table></div>
    </div>
  </div>

  <!-- TAB: 키워드 생성 -->
  <div id="tab-keygen" class="tab-pane active">
    <div class="card">
      <div class="clabel">블로그 글 제목 입력</div>
      <div class="info-box">
        글 제목을 한 줄에 하나씩 붙여넣기 하면 <strong>키워드 | 글 제목</strong> 형식으로 자동 변환합니다.<br>
        변환 후 <strong>➡ 검색 탭으로 바로 보내기</strong>를 누르면 검색 탭에 바로 입력됩니다.
      </div>
      <textarea id="titleInp" placeholder="봉명동선불유심 약정 없이 쓰기 좋은 이유 세 가지
산남동선불폰 개통 전에 꼭 알아야 할 것들
내덕동선불폰 개통 간편인증서 6종 중 뭘 써야 할까요" oninput="onTitleInp()"></textarea>
      <div class="bar">
        <span class="hint">※ 제목만 한 줄씩 입력하세요</span>
        <span class="cnt" id="tcnt">0줄</span>
      </div>
      <div class="kw-opts">
        <label>키워드 단어 수:</label>
        <div class="kw-seg" id="kwSeg">
          <button onclick="setKwN(1,this)" class="sel">1</button>
          <button onclick="setKwN(2,this)">2</button>
          <button onclick="setKwN(3,this)">3</button>
          <button onclick="setKwN(4,this)">4</button>
          <button onclick="setKwN(5,this)">5</button>
        </div>
        <span id="kwHint" style="font-size:12px;color:var(--mu)">제목 첫 단어</span>
      </div>
      <div class="actions">
        <button class="btn" onclick="genKeywords()">✨ 키워드 생성</button>
      </div>
    </div>
    <div class="card" id="genResultCard" style="display:none">
      <div class="clabel">변환 결과 — <span id="genCntLabel" style="color:var(--g)"></span></div>
      <div class="preview-rows" id="previewRows"></div>
      <div style="margin-top:14px;font-size:12px;color:var(--mu);margin-bottom:6px">아래 텍스트를 복사하거나 검색 탭으로 바로 보내세요.</div>
      <textarea class="out-ta" id="genOut" spellcheck="false"></textarea>
      <div class="actions">
        <button class="btn btn-sm" onclick="copyGen()">📋 클립보드 복사</button>
        <button class="btn btn-sm btn-blue" onclick="sendToSearch()">➡ 검색 탭으로 바로 보내기</button>
      </div>
    </div>
  </div>

  <!-- TAB: 블로그 설정 -->
  <div id="tab-config" class="tab-pane">
    <div class="card">
      <div class="clabel">내 블로그 아이디 등록</div>
      <div class="info-box">
        네이버 블로그 주소에서 아이디를 확인하세요: <strong>blog.naver.com/<em>아이디</em>/12345</strong><br>
        아이디를 등록하면 검색결과에서 <strong>내 글인지 정확하게 판정</strong>합니다.<br>
        여러 블로그를 운영하면 아이디를 여러 개 추가할 수 있습니다.
      </div>
      <div style="display:flex;gap:8px;align-items:flex-start;margin-bottom:12px">
        <input type="text" id="idInput" placeholder="블로그 아이디 입력 (예: mynaverId)"
          style="flex:1;padding:9px 14px;border:1.5px solid var(--bd);border-radius:8px;font-size:13px;outline:none;font-family:inherit"
          onkeydown="if(event.key==='Enter'){addBlogId();event.preventDefault()}"
          onfocus="this.style.borderColor='var(--g)'" onblur="this.style.borderColor='var(--bd)'">
        <button class="btn btn-sm" onclick="addBlogId()" style="white-space:nowrap">+ 추가</button>
      </div>
      <div id="chipArea" class="id-chips" style="min-height:40px;border:1.5px solid var(--bd);border-radius:10px;padding:8px 10px;background:#fafafa">
        <span id="noIdMsg" style="font-size:12px;color:var(--mu);align-self:center">등록된 아이디 없음 — 제목 유사도로 판정합니다</span>
      </div>
      <div style="margin-top:14px;font-size:12px;color:var(--mu)">
        ✅ 저장은 자동입니다 — 브라우저를 닫고 다시 열어도 유지됩니다.
      </div>
    </div>
    <div class="card">
      <div class="clabel">판정 방식 비교</div>
      <table style="font-size:12px">
        <thead><tr>
          <th style="width:140px">구분</th><th>블로그 아이디 등록 ✅</th><th>아이디 없음</th>
        </tr></thead>
        <tbody>
          <tr><td style="font-weight:700">판정 방법</td><td>내 블로그 URL 포함 여부</td><td>제목 단어 유사도</td></tr>
          <tr><td style="font-weight:700">오탐 가능성</td><td style="color:#1b5e20">거의 없음</td><td style="color:#c62828">있음 (다른 글이 매칭될 수 있음)</td></tr>
          <tr><td style="font-weight:700">미탐 가능성</td><td style="color:#795548">제목이 달라도 내 글 모두 감지</td><td style="color:#795548">제목 일치 필요</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const COLORS={
  'VIEW':{bg:'#e8f5e9',tx:'#1b5e20',bd:'#a5d6a7'},
  '\ube14\ub85c\uadf8':{bg:'#e8f5e9',tx:'#2e7d32',bd:'#c8e6c9'},
  '\ub274\uc2a4':{bg:'#fff8e1',tx:'#e65100',bd:'#ffe082'},
  '\uce74\ud398':{bg:'#fce4ec',tx:'#880e4f',bd:'#f48fb1'},
  '\uc9c0\uc2ddIN':{bg:'#ede7f6',tx:'#4527a0',bd:'#b39ddb'},
  '\uc6f9\uc0ac\uc774\ud2b8':{bg:'#e8eaf6',tx:'#283593',bd:'#9fa8da'},
  '\ub3d9\uc601\uc0c1':{bg:'#ffebee',tx:'#b71c1c',bd:'#ef9a9a'},
  '\uc1fc\ud551':{bg:'#fff3e0',tx:'#bf360c',bd:'#ffcc80'},
  '\ud3ec\uc2a4\ud2b8':{bg:'#e0f7fa',tx:'#006064',bd:'#80deea'},
};
const DEF={bg:'#f5f5f5',tx:'#555',bd:'#e0e0e0'};
function mk(s){const c=COLORS[s]||DEF;return`<span class="tag" style="background:${c.bg};color:${c.tx};border:1px solid ${c.bd}">${e(s)}</span>`}
function e(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2200)}

// ── 블로그 아이디 관리 ──────────────────────────────
const LS_KEY = 'naver_blog_ids_v1';
let blogIds = [];

function loadIds(){
  try { blogIds = JSON.parse(localStorage.getItem(LS_KEY)||'[]'); } catch{blogIds=[];}
  renderChips(); updateIdStatus();
}
function saveIds(){
  localStorage.setItem(LS_KEY, JSON.stringify(blogIds));
  renderChips(); updateIdStatus();
}
function addBlogId(){
  const v = document.getElementById('idInput').value.trim().toLowerCase();
  if(!v){toast('\uc544\uc774\ub514\ub97c \uc785\ub825\ud574\uc8fc\uc138\uc694');return;}
  if(blogIds.includes(v)){toast('\uc774\ubbf8 \ub4f1\ub85d\ub41c \uc544\uc774\ub514\uc785\ub2c8\ub2e4');return;}
  blogIds.push(v);
  document.getElementById('idInput').value='';
  saveIds();
  toast(`"${v}" \ub4f1\ub85d\ub410\uc2b5\ub2c8\ub2e4 \u2714`);
}
function removeId(id){
  blogIds = blogIds.filter(x=>x!==id);
  saveIds();
}
function renderChips(){
  const area = document.getElementById('chipArea');
  const noMsg = document.getElementById('noIdMsg');
  if(blogIds.length===0){
    area.innerHTML='';
    area.appendChild(noMsg);
    return;
  }
  area.innerHTML = blogIds.map(id=>
    `<span class="id-badge">blog.naver.com/<strong>${e(id)}</strong>
      <span class="del" onclick="removeId('${e(id)}')" title="\uc81c\uac70">\u00d7</span>
    </span>`
  ).join('');
}
function updateIdStatus(){
  const hasIds = blogIds.length > 0;
  document.getElementById('idModeBadge').style.display = hasIds ? '' : 'none';
  document.getElementById('ttlModeBadge').style.display = hasIds ? 'none' : '';
  // 상단 검색탭 chips
  const sc = document.getElementById('savedChips');
  sc.innerHTML = hasIds ? blogIds.map(id=>
    `<span class="id-badge" style="font-size:11px">${e(id)}</span>`
  ).join('') : '';
}

// ── 탭 전환 ──────────────────────────────
function switchTab(id,btn){
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+id).classList.add('active');
  if(btn) btn.classList.add('active');
  else document.querySelectorAll('.tab-btn')[[['keygen','search','config'].indexOf(id)]].classList.add('active');
}

// ── 검색 확인 ──────────────────────────────
function getLines(){return document.getElementById('inp').value.split(/\n/).map(l=>l.trim()).filter(l=>l);}
function onInp(){document.getElementById('cnt').textContent=getLines().length+'\uc904';}
let allRes=[];

async function go(){
  const lines=getLines();
  if(!lines.length){alert('\uc785\ub825\ud574\uc8fc\uc138\uc694.');return;}
  allRes=new Array(lines.length).fill(null);
  const total=lines.length;
  document.getElementById('startBtn').disabled=true;
  document.getElementById('progCard').style.display='block';
  document.getElementById('resCard').style.display='block';
  document.getElementById('rsub').textContent='';
  setProg(0,total,'');
  const tbody=document.getElementById('tbody');
  tbody.innerHTML='';
  lines.forEach((line,i)=>{
    const parts=line.includes('|')?line.split('|',2):[line,line];
    const kw=parts[0].trim(),ttl=(parts[1]||parts[0]).trim();
    const tr=document.createElement('tr');tr.id='r'+i;tr.className='row-wait';
    const kwUrl='https://search.naver.com/search.naver?query='+encodeURIComponent(kw);
    const ttlUrl='https://search.naver.com/search.naver?query='+encodeURIComponent(ttl);
    tr.innerHTML=`<td class="tn">${i+1}</td>
      <td class="tkw">${e(kw)}<div class="nsr">
        <a class="nsr-btn kw" href="${kwUrl}" target="_blank" rel="noopener">\ud0a4\uc6cc\ub4dc\u2197</a>
        <a class="nsr-btn ttl" href="${ttlUrl}" target="_blank" rel="noopener">\uc81c\ubaa9\u2197</a>
      </div></td>
      <td class="ttl">${e(ttl)}</td>
      <td colspan="2" style="color:var(--mu)"><span class="spin"></span>\ubd84\uc11d \uc911...</td>`;
    tbody.appendChild(tr);
  });
  const r=await fetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({lines, blog_ids: blogIds})});
  const data=await r.json();
  const {job_id}=data;
  if(data.blog_id_mode) toast('\ubd14\ub85c\uadf8 \uc544\uc774\ub514 \ubaa8\ub4dc\ub85c \uc815\ud655 \ud310\uc815\ud569\ub2c8\ub2e4 \uD83C\uDFAF');
  const es=new EventSource('/stream/'+job_id);
  const idx={};
  lines.forEach((l,i)=>{const p=l.includes('|')?l.split('|',2):[l,l];idx[p[0].trim()]=i;});
  let done=0;
  es.onmessage=ev=>{
    if(ev.data==='[DONE]'||ev.data==='[TIMEOUT]'){es.close();onDone(done,total);return;}
    const res=JSON.parse(ev.data);
    const i=idx[res.keyword]??done;
    allRes[i]=res;done++;
    setProg(done,total,res.keyword);
    drawRow(i,res);
  };
  es.onerror=()=>{es.close();onDone(done,total);};
}
function setProg(done,total,kw){
  const p=total?Math.round(done/total*100):0;
  document.getElementById('pb').style.width=p+'%';
  document.getElementById('pdone').textContent=done;
  document.getElementById('ptot').textContent=total;
  document.getElementById('pkw').textContent=kw?'\uc644\ub8cc: '+kw.slice(0,50):(done===total?'\u2713 \ubaa8\ub450 \uc644\ub8cc':'');
}
function drawRow(i,r){
  const tr=document.getElementById('r'+i);if(!tr)return;
  tr.className=r.error?'row-err':'';
  const pcHTML=r.pc&&r.pc.length?r.pc.map(mk).join(''):'<span class="tag-ng">\u274c \ubbf8\ub178\ucd9c</span>';
  const mobHTML=r.mobile&&r.mobile.length?r.mobile.map(mk).join(''):'<span class="tag-ng">\u274c \ubbf8\ub178\ucd9c</span>';
  const errHTML=r.error?`<div style="color:#e53935;font-size:11px;margin-top:4px">\u26a0 ${e(r.error)}</div>`:'';
  const kwUrl='https://search.naver.com/search.naver?query='+encodeURIComponent(r.keyword);
  const ttlUrl='https://search.naver.com/search.naver?query='+encodeURIComponent(r.title);
  tr.innerHTML=`<td class="tn">${i+1}</td>
    <td class="tkw">${e(r.keyword)}<div class="nsr">
      <a class="nsr-btn kw" href="${kwUrl}" target="_blank" rel="noopener">\ud0a4\uc6cc\ub4dc\u2197</a>
      <a class="nsr-btn ttl" href="${ttlUrl}" target="_blank" rel="noopener">\uc81c\ubaa9\u2197</a>
    </div></td>
    <td class="ttl">${e(r.title)}</td>
    <td class="ts"><div class="tags">${pcHTML}</div>${errHTML}</td>
    <td class="ts"><div class="tags">${mobHTML}</div></td>`;
}
function onDone(done,total){
  document.getElementById('startBtn').disabled=false;
  document.getElementById('rsub').textContent='\uc5f4 '+done+'/'+total+'\uac1c \uc644\ub8cc';
  setProg(done,total,'');
}
function exportCSV(){
  const rows=[['#','\uac80\uc0c9\ud0a4\uc6cc\ub4dc','\uae00\uc81c\ubaa9','PC\ub178\ucd9c','\ubaa8\ubc14\uc77c\ub178\ucd9c','\uc624\ub958']];
  allRes.forEach((r,i)=>{if(r)rows.push([i+1,r.keyword,r.title,(r.pc||[]).join('/'),(r.mobile||[]).join('/'),r.error||'']);});
  const csv=rows.map(r=>r.map(c=>'"'+String(c).replace(/"/g,'""')+'"').join(',')).join('\n');
  const blob=new Blob(['\uFEFF'+csv],{type:'text/csv;charset=utf-8'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='naver_'+new Date().toISOString().slice(0,10)+'.csv';a.click();
}

// ── 키워드 생성 ──────────────────────────────
let kwN=1;
function setKwN(n,btn){kwN=n;document.querySelectorAll('#kwSeg button').forEach(b=>b.classList.remove('sel'));btn.classList.add('sel');}
function getTitles(){return document.getElementById('titleInp').value.split(/\n/).map(l=>l.trim()).filter(l=>l);}
function onTitleInp(){document.getElementById('tcnt').textContent=getTitles().length+'\uc904';}
function genKeywords(){
  const titles=getTitles();
  if(!titles.length){alert('\uc81c\ubaa9\uc744 \uc785\ub825\ud574\uc8fc\uc138\uc694.');return;}
  const lines=titles.map(t=>t.split(/\s+/).slice(0,kwN).join('')+' | '+t);
  document.getElementById('genOut').value=lines.join('\n');
  document.getElementById('previewRows').innerHTML=lines.map((line,i)=>{
    const idx=line.indexOf(' | ');
    return '<div class="preview-row"><div class="pr-num">'+(i+1)+'</div><div class="pr-kw">'+e(line.slice(0,idx))+'</div><div class="pr-sep">|</div><div class="pr-ttl">'+e(line.slice(idx+3))+'</div></div>';
  }).join('');
  document.getElementById('genCntLabel').textContent=lines.length+'\uac1c \ubcc0\ud658';
  document.getElementById('genResultCard').style.display='block';
}
function copyGen(){
  const ta=document.getElementById('genOut');ta.select();document.execCommand('copy');
  window.getSelection().removeAllRanges();toast('\ud074\ub9bd\ubcf4\ub4dc \ubcf5\uc0ac!');
}
function sendToSearch(){
  const val=document.getElementById('genOut').value.trim();
  if(!val){alert('\uba3c\uc800 \ud0a4\uc6cc\ub4dc\ub97c \uc0dd\uc131\ud558\uc138\uc694.');return;}
  document.getElementById('inp').value=val;onInp();switchTab('search',null);
  toast('\uac80\uc0c9 \ud0ed\uc73c\ub85c \uc804\uc1a1!');
}
// 초기화
loadIds();onInp();onTitleInp();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print("="*50)
    print(f"  Naver Search Checker  ->  http://localhost:{port}")
    print("="*50)
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
