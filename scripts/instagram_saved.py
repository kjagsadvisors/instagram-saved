#!/usr/bin/env python3
"""Export own Instagram saves via Context.dev browser profiles. Python 3.10+."""
import argparse, csv, getpass, html, io, json, os, re, sys, tempfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib import request, error, parse
API = "https://api.context.dev/v1/web/scrape"
CONFIG = Path.home() / ".config/instagram-saved/profile.json"

class ExportError(Exception):
    pass

def now():
    return datetime.now(timezone.utc).isoformat()

def private_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ig-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def canonical_post(value):
    try:
        u = parse.urlsplit(parse.urljoin("https://www.instagram.com/", value))
        if u.scheme != "https" or u.hostname not in {"instagram.com", "www.instagram.com"} or u.username or u.port not in (None, 443):
            return None
    except ValueError:
        return None
    m = re.fullmatch(r"/(?:[A-Za-z0-9_.]+/)?(p|reel|tv)/([A-Za-z0-9_-]+)/?", u.path)
    return f"https://www.instagram.com/{m[1]}/{m[2]}/" if m else None

def collection_url(username, supplied=None):
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", username):
        raise ExportError("INVALID_USERNAME")
    try:
        u = parse.urlsplit(supplied or f"https://www.instagram.com/{username}/saved/all-posts/")
        valid = (u.scheme == "https" and u.hostname in {"instagram.com", "www.instagram.com"}
                 and not u.username and u.port in (None, 443) and not u.query and not u.fragment
                 and u.path.startswith(f"/{username}/saved/")
                 and re.fullmatch(r"/[A-Za-z0-9_.]+/saved/[A-Za-z0-9_-]+/", u.path))
    except ValueError:
        valid = False
    if not valid:
        raise ExportError("USE_OWN_ACCOUNT_SAVED_COLLECTION_URL")
    return "https://www.instagram.com" + u.path

class SavedHTML(HTMLParser):
    VOID = {"area","base","br","col","embed","hr","img","input","link","meta","param","source","track","wbr"}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.posts, self.text = [], {}, []
        self.main_seen, self.has_password, self.active = False, False, None
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("type") == "password":
            self.has_password = True
        main, ignore = self.stack[-1][1:] if self.stack else (False, False)
        main = main or tag == "main" or a.get("role") == "main"
        ignore = ignore or tag in {"script","style","nav","header","footer"}
        self.main_seen |= main
        if tag not in self.VOID:
            self.stack.append((tag, main, ignore))
        if not main or ignore:
            return
        if tag == "a":
            self.active = canonical_post(a.get("href", ""))
            if self.active:
                self.posts.setdefault(self.active, {"url": self.active, "alt_text": None})
        if tag == "img" and self.active and a.get("alt"):
            self.posts[self.active]["alt_text"] = a["alt"]
    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)
    def handle_endtag(self, tag):
        if tag == "a":
            self.active = None
        for i in range(len(self.stack)-1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
    def handle_data(self, data):
        if self.stack and self.stack[-1][1] and not self.stack[-1][2]:
            self.text.append(data)

def extract_saved(response, expected):
    if not isinstance(response, dict):
        raise ExportError("INVALID_API_RESPONSE")
    output = response.get("html") or {}
    if not isinstance(output, dict) or output.get("success") is not True or not isinstance(output.get("data"), str):
        raise ExportError("HTML_OUTPUT_FAILED")
    meta = response.get("metadata")
    if meta is not None and not isinstance(meta, dict):
        raise ExportError("INVALID_API_METADATA")
    resolved = (meta or {}).get("finalUrl") or response.get("url") or expected
    if not isinstance(resolved, str):
        raise ExportError("INVALID_FINAL_URL")
    try:
        u = parse.urlsplit(resolved)
    except ValueError:
        raise ExportError("INVALID_FINAL_URL") from None
    if u.hostname not in {"instagram.com","www.instagram.com"}:
        raise ExportError("UNEXPECTED_ORIGIN")
    if any(s in u.path.lower() for s in ("/accounts/","/challenge/","/checkpoint/")):
        raise ExportError("INSTAGRAM_LOGIN_OR_CHALLENGE")
    if u.path.rstrip("/") != parse.urlsplit(expected).path.rstrip("/"):
        raise ExportError("UNVERIFIED_SAVED_VIEW")
    p = SavedHTML()
    p.feed(output["data"])
    text = " ".join(p.text).lower()
    if p.has_password or any(s in text for s in ("confirm it's you","enter your security code",
            "suspicious login attempt","automated behavior","try again later","confirm you're human")):
        raise ExportError("INSTAGRAM_LOGIN_OR_CHALLENGE")
    marker = bool(re.search(r"\bsaved\b|\ball posts\b", text))
    if not p.main_seen or not marker:
        raise ExportError("UNVERIFIED_SAVED_VIEW")
    if not p.posts:
        raise ExportError("NO_SAVED_POST_LINKS_FOUND")
    return list(p.posts.values())

class ContextClient:
    def __init__(self, key, budget):
        if not key:
            raise ExportError("SET_CONTEXT_DEV_API_KEY_LOCALLY")
        self.key, self.budget, self.calls = key, budget, 0
    def scrape(self, body):
        if self.calls >= self.budget:
            raise ExportError("REQUEST_BUDGET_REACHED")
        self.calls += 1
        req = request.Request(API, json.dumps(body).encode(), headers={
            "Authorization":"Bearer "+self.key,"Content-Type":"application/json"}, method="POST")
        try:
            with request.urlopen(req, timeout=150) as r:
                result = json.load(r)
        except error.HTTPError as e:
            try:
                code = json.loads(e.read()).get("error_code", f"HTTP_{e.code}")
            except (ValueError, AttributeError):
                code = f"HTTP_{e.code}"
            code = code if isinstance(code,str) and re.fullmatch(r"[A-Z0-9_]{1,80}",code) else f"HTTP_{e.code}"
            raise ExportError(code) from None
        except (error.URLError, TimeoutError, OSError):
            raise ExportError("NETWORK_OR_TIMEOUT_ERROR") from None
        except ValueError:
            raise ExportError("INVALID_API_JSON") from None
        if not isinstance(result, dict):
            raise ExportError("INVALID_API_RESPONSE")
        return result

def saved_body(url, profile, depth=0):
    shared = {"mainContentOnly":False,"profile":profile}
    if depth:
        shared["actions"] = [{"type":"scroll","direction":"down","amount":"viewport","maxScrolls":depth},
                             {"type":"wait","milliseconds":1500}]
    return {"url":url,"formats":{"html":True},"sharedParams":shared,"timeoutOpts":{"milliseconds":120000}}

def new_record(found, rank):
    return {**found,"shortcode":found["url"].rstrip("/").split("/")[-1],"observed_rank":rank,
        "creator":None,"caption":None,"posted_at":None,"type":None,"like_count":None,
        "comment_count":None,"view_count":None,"metadata_status":"not_requested","metadata_error":None}

def enrich(client, record):
    result = client.scrape({"url":record["url"],"formats":{"parse":True},
        "parseParams":{"type":"prebuilt","parser":"instagram-post"}}).get("parsed") or {}
    if not isinstance(result, dict) or result.get("success") is not True or not isinstance(result.get("data"),dict):
        record["metadata_status"] = "unavailable"
        code = result.get("error_code") if isinstance(result, dict) else None
        record["metadata_error"] = code if isinstance(code,str) and re.fullmatch(r"[A-Z0-9_]{1,80}",code) else "PUBLIC_METADATA_UNAVAILABLE"
        return
    data = result["data"]
    if data.get("shortcode") != record["shortcode"]:
        raise ExportError("POST_METADATA_MISMATCH")
    record["creator"] = (data.get("owner") or {}).get("username")
    for f in ("caption","posted_at","type","like_count","comment_count","view_count"):
        record[f] = data.get(f)
    record["metadata_status"] = "available"

def csv_safe(value):
    text = "" if value is None else str(value)
    return "'"+text if text.lstrip().startswith(("=","+","-","@")) or text.startswith(("\t","\r","\n")) else text

def export_files(directory, records, run):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    private_write(directory/"saved-posts.json", json.dumps({"run":run,"posts":records},ensure_ascii=False,indent=2))
    buf = io.StringIO(newline="")
    fields = list(new_record({"url":"","alt_text":None},0))
    writer = csv.DictWriter(buf, fields)
    writer.writeheader()
    for r in records:
        writer.writerow({k:csv_safe(r.get(k)) for k in fields})
    private_write(directory/"saved-posts.csv",buf.getvalue())
    md = ["# Instagram saved posts","",f"Collected: {len(records)} | Stop: {run['stop_reason']}","",
          "Partial capture; save dates and complete coverage are unknown.",""]
    cards = []
    for r in records:
        title = "@"+str(r["creator"]) if r["creator"] else r["shortcode"]
        caption = r["caption"] or r["alt_text"] or "No caption available"
        clean = lambda s: re.sub(r"([\\\x60*_{}\[\]<>#])",r"\\\1",str(s))
        md.extend([f"## {r['observed_rank']}. {clean(title)}",r["url"],"",clean(caption),""])
        cards.append('<article><small>'+str(r["observed_rank"])+' · '+html.escape(r["metadata_status"])+
            '</small><h2>'+html.escape(title)+'</h2><p>'+html.escape(str(caption))+
            '</p><p><small>Image alt text: '+html.escape(str(r["alt_text"] or "Unavailable"))+'</small>'+
            '</p><a target="_blank" rel="noopener noreferrer" href="'+html.escape(r["url"],quote=True)+'">Open on Instagram ↗</a></article>')
    private_write(directory/"saved-posts.md","\n".join(md))
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Instagram Saved Library</title><style>body{background:#101016;color:#eee;font:16px system-ui;margin:0;padding:5vw;max-width:1300px}h1{font-size:clamp(32px,5vw,60px);margin-bottom:8px}header p{color:#b8b8c8}input{box-sizing:border-box;width:100%;padding:16px;border:1px solid #505064;background:#20202b;color:#fff;border-radius:12px;font:inherit;margin:24px 0}main{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:18px}article{min-width:0;background:#20202b;border:1px solid #363646;border-radius:16px;padding:24px}article[hidden]{display:none}h2{font-size:20px;overflow-wrap:anywhere}article p{white-space:pre-wrap;overflow-wrap:anywhere;max-height:240px;overflow:auto;line-height:1.6}a{color:#bda8ff}small{color:#aaa}footer{margin-top:30px;color:#aaa}</style>
<header><small>YOUR PRIVATE INSPIRATION LIBRARY</small><h1>Saved for a reason.</h1><p>__COUNT__ posts · __STOP__ · Partial capture</p></header><label for="q">Search captions, creators, or alt text</label><input id="q" type="search" placeholder="Try AI, construction, or a creator…"><p id="count" aria-live="polite"></p><main>__CARDS__</main><footer>Captions are not transcripts. No media downloaded. Complete coverage is unknown.</footer><script>const q=document.getElementById('q'),cards=[...document.querySelectorAll('article')],count=document.getElementById('count');function filter(){let n=0;for(const c of cards){c.hidden=!c.textContent.toLowerCase().includes(q.value.toLowerCase());if(!c.hidden)n++}count.textContent=n+' posts shown'}q.addEventListener('input',filter);filter();</script></html>'''
    private_write(directory/"index.html",page.replace("__COUNT__",str(len(records))).replace("__STOP__",html.escape(run["stop_reason"])).replace("__CARDS__","".join(cards)))

def pull(client, username, profile_id, output, limit=20, max_depth=6, supplied_url=None, metadata=False):
    url = collection_url(username,supplied_url)
    profile = {"type":"existing","profileId":profile_id}
    records, seen, stalled = [], set(), 0
    run = {"source_url":url,"captured_at":now(),"coverage":"partial","stop_reason":"running",
           "requests":0,"errors":[],"live_validation":"not_independently_verified"}
    try:
        for depth in range(max_depth+1):
            if client.calls >= client.budget:
                run["stop_reason"] = "request_budget"
                break
            found = extract_saved(client.scrape(saved_body(url,profile,depth)),url)
            before = len(records)
            for item in found:
                if item["url"] not in seen and len(records)<limit:
                    seen.add(item["url"])
                    records.append(new_record(item,len(records)+1))
            run.update(requests=client.calls,captured_count=len(records))
            export_files(output,records,run)
            if len(records)>=limit:
                run["stop_reason"] = "requested_limit"
                break
            stalled = stalled+1 if len(records)==before else 0
            if stalled>=2:
                run["stop_reason"] = "no_new_links"
                break
        else:
            run["stop_reason"] = "scroll_depth_limit"
        if metadata:
            for record in records:
                if client.calls >= client.budget:
                    run["errors"].append("METADATA_REQUEST_BUDGET_REACHED")
                    break
                try:
                    enrich(client,record)
                except ExportError as e:
                    record["metadata_status"] = "unavailable"
                    record["metadata_error"] = str(e)
                    raise
                run["requests"] = client.calls
                export_files(output,records,run)
    except ExportError as e:
        run["errors"].append(str(e))
        run["stop_reason"] = "error"
    finally:
        run.update(requests=client.calls,captured_count=len(records),finished_at=now())
        export_files(output,records,run)
    return run

def read_state(path):
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        data = {}
    except (OSError,ValueError):
        raise ExportError("INVALID_LOCAL_PROFILE_FILE") from None
    if not isinstance(data,dict):
        raise ExportError("INVALID_LOCAL_PROFILE_FILE")
    return data

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config",type=Path,default=CONFIG)
    sub = p.add_subparsers(dest="command",required=True)
    sub.add_parser("doctor",help="Check local configuration without API calls")
    login = sub.add_parser("login",help="Prompt locally for password; create profile")
    login.add_argument("--username",required=True)
    get = sub.add_parser("pull",help="Export bounded own saved-post links")
    get.add_argument("--collection-url")
    get.add_argument("--limit",type=int,default=20)
    get.add_argument("--max-requests",type=int,default=8)
    get.add_argument("--max-depth",type=int,default=6)
    get.add_argument("--enrich",action="store_true")
    get.add_argument("--output",type=Path,default=Path("instagram-saved-export"))
    sub.add_parser("forget",help="Delete local profile reference only")
    args = p.parse_args(argv)
    try:
        if args.command=="forget":
            args.config.unlink(missing_ok=True)
            print("Local reference removed. Provider-side session was not revoked.")
            return 0
        state = read_state(args.config)
        key = os.getenv("CONTEXT_DEV_API_KEY")
        username = os.getenv("INSTAGRAM_USERNAME") or state.get("username")
        profile_id = os.getenv("CONTEXT_DEV_PROFILE_ID") or state.get("profile_id")
        if args.command=="doctor":
            print(json.dumps({"python_supported":sys.version_info>=(3,10),"api_key_set":bool(key),
                "profile_configured":bool(profile_id and username),"paid_plan_required":True,"live_instagram_test":"not_run"}))
            return 0
        if args.command=="login":
            url = collection_url(args.username)
            client = ContextClient(key,1)
            if not sys.stdin.isatty():
                raise ExportError("RUN_LOGIN_YOURSELF_IN_AN_INTERACTIVE_TERMINAL")
            password = getpass.getpass("Instagram password (sent to Context.dev; not saved locally): ")
            if not password:
                raise ExportError("PASSWORD_REQUIRED")
            body = saved_body(url,{"type":"new","identifier":args.username,"password":password,
                                   "description":"Instagram saved-post export"})
            try:
                response = client.scrape(body)
            finally:
                body["sharedParams"]["profile"].pop("password",None)
                password = None
            profile_id = (response.get("profile") or {}).get("id")
            if not isinstance(profile_id,str) or not profile_id.startswith("prof_"):
                raise ExportError("NO_PROFILE_ID_RETURNED")
            private_write(args.config,json.dumps({"username":args.username,"profile_id":profile_id}))
            extract_saved(response,url)
            print("Profile stored; saved links detected. Run pull to export.")
            return 0
        if not username or not profile_id:
            raise ExportError("RUN_LOGIN_OR_CONFIGURE_EXISTING_PROFILE")
        if not (1<=args.limit<=500 and 1<=args.max_requests<=600 and 0<=args.max_depth<=50):
            raise ExportError("INVALID_LIMITS")
        client = ContextClient(key,args.max_requests)
        run = pull(client,username,profile_id,args.output,args.limit,args.max_depth,args.collection_url,args.enrich)
        print(json.dumps({"captured_count":run["captured_count"],"requests":run["requests"],
            "coverage":run["coverage"],"stop_reason":run["stop_reason"],"errors":run["errors"],"output":str(args.output.resolve())}))
        return 1 if run["errors"] else 0
    except ExportError as e:
        print("Error: "+str(e),file=sys.stderr)
        return 1
    except (OSError,KeyboardInterrupt):
        print("Stopped: local I/O failure or interruption. No secrets printed.",file=sys.stderr)
        return 1

if __name__=="__main__":
    raise SystemExit(main())
